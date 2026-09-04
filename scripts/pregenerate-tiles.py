#!/usr/bin/env python3
"""Pregenerate XYZ tiles for the dark-basemap stack.

Runs on the home box next to the renderer (QGIS Server on tcp/8081) and renders every
tile for a set of public layers into a durable store, /mnt/meow/OSM/tiles by default,
using the same `{layer}/{z}/{x}/{y}.png` layout the API and nginx serve.

The script is built for very long, interruptible runs:

  * resumable        — tiles already on disk are skipped, never re-rendered
  * atomic           — writes go to a sibling .tmp then rename, so the API's janitor
                       and any reader never see a half-written PNG
  * deduplicating    — byte-identical renders (ocean, empty land) are hardlinks to one
                       canonical file per (layer, zoom); all-same-pixel tiles collapse
                       to a canonical blank, so terabytes become megabytes
  * failure-tolerant — one bad tile never stops the run; failures are logged to a
                       JSONL file and retried with --retry-failed
  * honest           — --estimate prints tile counts and an ETA at a measured tile
                       rate before a single render is issued

It renders *metatiles*, not tiles: an n x n block in one WMS GetMap, plus a buffer that
is cropped away, then cut into 256px tiles. This has to match what the API does on a
cache miss (src/metatile.rs) — QGIS labels each request against the extent it is given,
so a tile rendered on its own has every label crossing its edge cut in half, and a store
built that way would serve broken labels no matter what the API does. METATILE_SIZE and
METATILE_BUFFER_PX come from the same .env the API reads.

The metatile path needs Pillow for the slicing; `--metatile 1` is the old
tile-at-a-time path and stays pure stdlib.

Usage:
  scripts/pregenerate-tiles.py --dry-run --max-zoom 3          # what would be done
  scripts/pregenerate-tiles.py --estimate --max-zoom 8         # counts + ETA, no renders
  scripts/pregenerate-tiles.py --max-zoom 15                   # the real thing (z0..15)
  scripts/pregenerate-tiles.py --retry-failed                  # re-attempt logged failures

Configuration comes from the repo root's .env (TILE_LAYER_ROUTES, PROJECT_VERSION,
TILES_STORE_DIR, RENDERER_PUBLISH_PORT, RENDER_TIMEOUT_SECS, METATILE_SIZE,
METATILE_BUFFER_PX) and can be overridden by flags or environment variables.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import io
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PROJECT_VERSION_MARKER = ".project-version"
FAILURES_FILE = ".pregenerate-failures.jsonl"
INDEX_DB = ".pregenerate-index.db"
BLANK_DIR = ".blank"

DEFAULT_RENDERER_PORT = 8081
DEFAULT_TPS = 1.5  # measured on the live renderer: 5 concurrent renders serialize to ~1.1 tiles/s
TILE_PX = 256
# Edge of the Web-Mercator square in metres; mirrors MERCATOR_HALF_SPAN in src/metatile.rs.
MERCATOR_HALF_SPAN = 20037508.342789244
DEFAULT_METATILE = "0-6:2,7-20:4"
DEFAULT_METATILE_BUFFER = 128


# ---------------------------------------------------------------------------------------
# .env and layer routing (semantics mirrored from src/config.rs and src/tiles.rs)


def load_env(path):
    """Reads KEY=VALUE lines, skipping comments; values may be double- or single-quoted."""
    env = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                env[key.strip()] = value
    except FileNotFoundError:
        pass
    return env


def is_safe_layer_name(name):
    """Same conservative charset as src/tiles.rs: this string becomes a path segment."""
    return bool(name) and name not in (".", "..") and all(
        c.isascii() and (c.isalnum() or c in "_-.") for c in name
    )


def parse_routes(raw):
    """Parses TILE_LAYER_ROUTES exactly like LayerRoutes::from_str in src/config.rs.

    Format: `public@lo-hi=upstream,...`. First matching rule wins; anything unrouted
    renders from its own name. A malformed rule is an error, never silently ignored.
    """
    routes = []
    for rule in raw.split(","):
        rule = rule.strip()
        if not rule:
            continue
        if "=" not in rule or "@" not in rule or "-" not in rule:
            raise ValueError(f"malformed route {rule!r}: expected '<public>@<lo>-<hi>=<upstream>'")
        left, _, upstream = rule.partition("=")
        public, _, zoom_range = left.partition("@")
        lo_raw, _, hi_raw = zoom_range.partition("-")
        public, upstream = public.strip(), upstream.strip()
        lo_raw, hi_raw = lo_raw.strip(), hi_raw.strip()
        for label, name in (("public layer", public), ("upstream layer", upstream)):
            if not is_safe_layer_name(name):
                raise ValueError(f"{label} {name!r} in route {rule!r} is not a valid layer name")
        try:
            lo, hi = int(lo_raw), int(hi_raw)
        except ValueError:
            raise ValueError(f"zoom bound in route {rule!r} is not a number 0-255") from None
        if not (0 <= lo <= hi < 32):
            raise ValueError(f"zoom range {lo}-{hi} in route {rule!r} is invalid or inverted")
        routes.append((public, lo, hi, upstream))
    return routes


def resolve_upstream(routes, public, z):
    for p, lo, hi, upstream in routes:
        if p == public and lo <= z <= hi:
            return upstream
    return public


def parse_metatile(raw):
    """Parses METATILE_SIZE exactly like MetatileSizes::from_str in src/config.rs.

    Format: `lo-hi:tiles-per-side,...`; first matching band wins, unbanded zooms render one
    tile at a time. Strict, because a band silently misread here would fill the store with
    tiles the API would then never agree with.
    """
    bands = []
    for rule in raw.split(","):
        rule = rule.strip()
        if not rule:
            continue
        if ":" not in rule or "-" not in rule:
            raise ValueError(f"malformed metatile band {rule!r}: expected '<lo>-<hi>:<tiles>'")
        zoom_range, _, size_raw = rule.partition(":")
        lo_raw, _, hi_raw = zoom_range.partition("-")
        try:
            lo, hi, size = int(lo_raw.strip()), int(hi_raw.strip()), int(size_raw.strip())
        except ValueError:
            raise ValueError(f"metatile band {rule!r} has a non-numeric field") from None
        if not (0 <= lo <= hi < 32):
            raise ValueError(f"zoom range {lo}-{hi} in band {rule!r} is invalid or inverted")
        if size not in (1, 2, 4, 8):
            raise ValueError(f"block size {size} in band {rule!r} must be 1, 2, 4 or 8")
        bands.append((lo, hi, size))
    return bands


def metatile_size(bands, z):
    """Block edge for this zoom, clamped to the pyramid. 1 means one tile per render."""
    for lo, hi, size in bands:
        if lo <= z <= hi:
            return max(1, min(size, 1 << z))
    return 1


def public_layers(routes, fallback):
    layers = [p for p, *_ in routes]
    if not layers:
        return [fallback]
    # Deduplicate while preserving order.
    return list(dict.fromkeys(layers))


# ---------------------------------------------------------------------------------------
# Minimal PNG inspection: is every pixel identical? (8-bit gray/RGB/RGBA, filters 0-4)


def png_is_uniform(data):
    """True when the PNG is 8-bit and every pixel (and alpha) is identical.

    Used to collapse empty ocean/land tiles into one canonical blank per (layer, zoom).
    Anything the decoder does not understand returns False and is stored as-is.
    """
    try:
        if not data.startswith(PNG_MAGIC):
            return False
        pos = 8
        width = height = bit_depth = color_type = interlace = None
        idat = bytearray()
        while pos + 8 <= len(data):
            (length,) = struct.unpack(">I", data[pos : pos + 4])
            ctype = data[pos + 4 : pos + 8]
            chunk = data[pos + 8 : pos + 8 + length]
            if ctype == b"IHDR" and length >= 13:
                width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                    ">IIBBBBB", chunk[:13]
                )
            elif ctype == b"IDAT":
                idat.extend(chunk)
            elif ctype == b"IEND":
                break
            pos += 12 + length
        if None in (width, height) or bit_depth != 8 or interlace != 0:
            return False
        if color_type not in (0, 2, 4, 6):
            return False
        bpp = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
        alpha_offsets = {4: range(1, bpp, 2), 6: range(3, bpp, 4)}.get(color_type, ())
        stride = width * bpp
        raw = zlib.decompress(bytes(idat))
        if len(raw) < (stride + 1) * height:
            return False
        prev = bytearray(stride)
        pixels = bytearray()
        for row in range(height):
            f = raw[row * (stride + 1)]
            line = bytearray(raw[row * (stride + 1) + 1 : (row + 1) * (stride + 1)])
            if f == 1:  # Sub
                for i in range(bpp, stride):
                    line[i] = (line[i] + line[i - bpp]) & 0xFF
            elif f == 2:  # Up
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif f == 3:  # Average
                for i in range(stride):
                    a = line[i - bpp] if i >= bpp else 0
                    line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
            elif f == 4:  # Paeth
                for i in range(stride):
                    a = line[i - bpp] if i >= bpp else 0
                    b = prev[i]
                    c = prev[i - bpp] if i >= bpp else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[i] = (line[i] + pr) & 0xFF
            elif f != 0:
                return False
            pixels.extend(line)
            prev = line
        first = pixels[:bpp]
        if all(pixels[i : i + bpp] == first for i in range(bpp, len(pixels), bpp)):
            return True
        # Fully transparent is blank too, whatever the hidden RGB bytes say.
        return alpha_offsets and all(
            all(pixels[i + off] == 0 for off in alpha_offsets) for i in range(0, len(pixels), bpp)
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------------------
# Dedup index (sqlite). Maps (layer, z, sha256) -> canonical path so identical renders
# become hardlinks instead of copies.


class Index:
    def __init__(self, db_path):
        self.db_path = db_path
        self._local = threading.local()

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass  # some FUSE filesystems reject WAL; the rollback journal still works
            conn.execute(
                "CREATE TABLE IF NOT EXISTS content ("
                " layer TEXT NOT NULL, z INTEGER NOT NULL,"
                " sha TEXT NOT NULL, path TEXT NOT NULL,"
                " PRIMARY KEY (layer, z, sha))"
            )
            self._local.conn = conn
        return conn

    def find(self, layer, z, sha):
        row = self._conn().execute(
            "SELECT path FROM content WHERE layer=? AND z=? AND sha=?", (layer, z, sha)
        ).fetchone()
        return row[0] if row else None

    def insert(self, layer, z, sha, path):
        self._conn().execute(
            "INSERT OR IGNORE INTO content (layer, z, sha, path) VALUES (?,?,?,?)",
            (layer, z, sha, path),
        )
        self._conn().commit()

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def rebuild_index(index, out_root, layers, min_zoom, max_zoom, log):
    """Hashes every existing tile in range and fills the index, so a resumed run keeps
    deduplicating against tiles written by earlier runs. O(files on disk)."""
    seen = 0
    for layer in layers:
        for z in range(min_zoom, max_zoom + 1):
            z_dir = os.path.join(out_root, layer, str(z))
            if not os.path.isdir(z_dir):
                continue
            for x_dir in sorted(os.listdir(z_dir)):
                x_path = os.path.join(z_dir, x_dir)
                if not os.path.isdir(x_path):
                    continue
                for name in sorted(os.listdir(x_path)):
                    if not name.endswith(".png"):
                        continue
                    path = os.path.join(x_path, name)
                    if os.path.getsize(path) == 0:
                        continue
                    try:
                        with open(path, "rb") as f:
                            head = f.read(8)
                        if head != PNG_MAGIC:
                            continue
                        with open(path, "rb") as f:
                            sha = hashlib.sha256(f.read()).hexdigest()
                        index.insert(layer, z, sha, path)
                        seen += 1
                        if seen % 50000 == 0:
                            log(f"index rebuild: {seen} tiles hashed")
                    except OSError:
                        continue
    log(f"index rebuild complete: {seen} tiles")


# ---------------------------------------------------------------------------------------
# Tile fetch + storage


class TileError(Exception):
    pass


def fetch_tile(base_url, upstream, z, x, y, timeout):
    """One WMTS GetTile; query shape copied from TileCoord::wmts_query in src/tiles.rs."""
    query = urllib.parse.urlencode(
        [
            ("SERVICE", "WMTS"),
            ("VERSION", "1.0.0"),
            ("REQUEST", "GetTile"),
            ("LAYER", upstream),
            ("STYLE", "default"),
            ("FORMAT", "image/png"),
            ("TILEMATRIXSET", "EPSG:3857"),
            ("TILEMATRIX", str(z)),
            ("TILECOL", str(x)),
            ("TILEROW", str(y)),
        ]
    )
    req = urllib.request.Request(
        f"{base_url}/ows/?{query}", headers={"User-Agent": "dark-basemap-pregenerate/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "").lower()
            data = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TileError(f"transport: {exc}") from exc
    # Body before header, like src/renderer.rs: a proxy can relabel anything.
    if "xml" in content_type or data.startswith(b"<"):
        raise TileError(f"service exception: {data[:200]!r}")
    if not content_type.startswith("image/png"):
        raise TileError(f"unexpected content-type {content_type!r}")
    if not data.startswith(PNG_MAGIC) or not data:
        raise TileError("empty or non-PNG body")
    return data


def metatile_bbox(z, mx, my, n, buffer_px):
    """EPSG:3857 extent of a block, grown by `buffer_px` on all four sides.

    Mirrors MetaCoord::bbox in src/metatile.rs; XYZ rows count from the north edge.
    """
    span = 2 * MERCATOR_HALF_SPAN / (1 << z)
    pad = buffer_px / TILE_PX * span
    return (
        -MERCATOR_HALF_SPAN + mx * span - pad,
        MERCATOR_HALF_SPAN - (my + n) * span - pad,
        -MERCATOR_HALF_SPAN + (mx + n) * span + pad,
        MERCATOR_HALF_SPAN - my * span + pad,
    )


def fetch_metatile(base_url, upstream, z, mx, my, n, buffer_px, timeout):
    """One WMS GetMap covering a whole block; query shape copied from MetaCoord::wms_query.

    WMS rather than WMTS because only GetMap takes an arbitrary extent. TRANSPARENT=TRUE is
    load-bearing: without it the background is opaque white where GetTile gives transparent,
    which paints a white sea.
    """
    min_x, min_y, max_x, max_y = metatile_bbox(z, mx, my, n, buffer_px)
    px = n * TILE_PX + 2 * buffer_px
    query = urllib.parse.urlencode(
        [
            ("SERVICE", "WMS"),
            ("VERSION", "1.1.1"),
            ("REQUEST", "GetMap"),
            ("LAYERS", upstream),
            ("STYLES", ""),
            ("FORMAT", "image/png"),
            ("TRANSPARENT", "TRUE"),
            ("SRS", "EPSG:3857"),
            ("BBOX", f"{min_x},{min_y},{max_x},{max_y}"),
            ("WIDTH", str(px)),
            ("HEIGHT", str(px)),
        ]
    )
    req = urllib.request.Request(
        f"{base_url}/ows/?{query}", headers={"User-Agent": "dark-basemap-pregenerate/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "").lower()
            data = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        # The renderer image's own nginx answers an overrun FastCGI read with a 404, because its
        # error_page target does not exist. A 404 here almost always means the block took longer
        # than its fastcgi_read_timeout, not that anything is malformed.
        raise TileError(f"transport: {exc}") from exc
    if "xml" in content_type or data.startswith(b"<"):
        raise TileError(f"service exception: {data[:200]!r}")
    if not content_type.startswith("image/png"):
        raise TileError(f"unexpected content-type {content_type!r}")
    if not data.startswith(PNG_MAGIC) or not data:
        raise TileError("empty or non-PNG body")
    return data


def slice_metatile(data, n, buffer_px):
    """Cuts a rendered block into `n*n` tiles, dropping the buffer.

    Yields `(dx, dy, png)` in row-major order. Pillow only for this: the stdlib decoder above
    unfilters a scanline at a time in Python, which is tolerable for the 256px blankness check
    and far too slow for a 1280px block.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - depends on the host
        sys.exit(
            "pregenerate: slicing metatiles needs Pillow (pip install pillow).\n"
            "Pass --metatile 1 to fall back to one GetTile per tile — but note that the API\n"
            "metatiles on a cache miss, so the store would then disagree with what it serves."
        )
    want = n * TILE_PX + 2 * buffer_px
    image = Image.open(io.BytesIO(data))
    if image.size != (want, want):
        raise TileError(f"renderer returned {image.size[0]}x{image.size[1]}, expected {want}x{want}")
    image = image.convert("RGBA")
    for dy in range(n):
        for dx in range(n):
            left, top = buffer_px + TILE_PX * dx, buffer_px + TILE_PX * dy
            buf = io.BytesIO()
            image.crop((left, top, left + TILE_PX, top + TILE_PX)).save(buf, format="PNG")
            yield dx, dy, buf.getvalue()


def fetch_metatile_with_retry(base_url, upstream, z, mx, my, n, buffer_px, timeout):
    try:
        return fetch_metatile(base_url, upstream, z, mx, my, n, buffer_px, timeout)
    except TileError as first:
        time.sleep(1)
        try:
            return fetch_metatile(base_url, upstream, z, mx, my, n, buffer_px, timeout)
        except TileError:
            raise first


def fetch_with_retry(base_url, upstream, z, x, y, timeout):
    """Transient hiccups (dead FCGI slot, brief timeout) get one immediate retry;
    anything else fails straight into the failure log."""
    try:
        return fetch_tile(base_url, upstream, z, x, y, timeout)
    except TileError as first:
        time.sleep(1)
        try:
            return fetch_tile(base_url, upstream, z, x, y, timeout)
        except TileError:
            raise first


def atomic_write(path, data):
    """Write-then-rename, mirroring TileCache::store in src/cache.rs. Accepts str or Path."""
    path = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def link_or_copy(src, dst):
    try:
        os.link(src, dst)
        return "linked"
    except OSError:
        shutil.copyfile(src, dst)
        return "copied"


def valid_existing(path):
    try:
        with open(path, "rb") as f:
            return f.read(8) == PNG_MAGIC
    except OSError:
        return False


def blank_canonical(out_root, layer, z):
    return os.path.join(out_root, BLANK_DIR, layer, f"{z}.png")


class Store:
    def __init__(self, out_root, index):
        self.out_root = out_root
        self.index = index

    def target(self, layer, z, x, y):
        return os.path.join(self.out_root, layer, str(z), str(x), f"{y}.png")

    def put(self, layer, z, x, y, data, sha):
        """Stores one tile. Returns (kind, stored_bytes)."""
        target = self.target(layer, z, x, y)
        os.makedirs(os.path.dirname(target), exist_ok=True)

        existing = self.index.find(layer, z, sha)
        if existing:
            kind = link_or_copy(existing, target)
            return f"dup-{kind}", 0

        if png_is_uniform(data):
            blank = blank_canonical(self.out_root, layer, z)
            if not valid_existing(blank):
                atomic_write(blank, data)
            kind = link_or_copy(blank, target)
            self.index.insert(layer, z, sha, blank)
            return f"blank-{kind}", 0

        atomic_write(target, data)
        self.index.insert(layer, z, sha, target)
        return "rendered", len(data)


# ---------------------------------------------------------------------------------------
# The run itself


def iter_blocks(layers, z, n):
    """Lazy row-major walk of the blocks at one zoom, for every layer; never materialised.

    `n == 1` degenerates to the plain tile walk this replaced.
    """
    span = 1 << z
    for layer in layers:
        for mx in range(0, span, n):
            for my in range(0, span, n):
                yield layer, z, mx, my, n


def count_tiles(layers, z):
    return len(layers) * (1 << (2 * z))


def fmt_duration(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def estimate_zoom(z, layers, tps):
    n = count_tiles(layers, z)
    return n, n / tps if tps else float("inf")


def probe_write_access(out_root):
    os.makedirs(out_root, exist_ok=True)
    probe = os.path.join(out_root, ".pregenerate-write-probe")
    try:
        atomic_write(probe, b"ok")
        os.unlink(probe)
    except OSError as exc:
        sys.exit(f"pregenerate: cannot write to {out_root}: {exc}")


def run(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="Pregenerate XYZ tiles (z0..z15 by default) into the durable tile store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--env-file", default=os.path.join(REPO_ROOT, ".env"),
                        help="path to the stack's .env (default: repo root)")
    parser.add_argument("--renderer-url", default=None,
                        help="QGIS Server base URL (default: http://127.0.0.1:<RENDERER_PUBLISH_PORT>)")
    parser.add_argument("--out-dir", default=None,
                        help="tile store root (default: TILES_STORE_DIR from .env, else /mnt/meow/OSM/tiles)")
    parser.add_argument("--layers", default=None, help="comma-separated public layers (default: from TILE_LAYER_ROUTES)")
    parser.add_argument("--min-zoom", type=int, default=0)
    parser.add_argument("--max-zoom", type=int, default=15)
    parser.add_argument("--concurrency", type=int, default=4,
                        help="concurrent renders; keep at or below the renderer's FCGID_MAX_PROCESSES")
    parser.add_argument("--timeout", type=float, default=None, help="per-render HTTP timeout in seconds")
    parser.add_argument("--metatile", default=None,
                        help="tiles per side per zoom band, e.g. '0-6:2,7-20:4' (default: "
                             "METATILE_SIZE from .env). Must match what the API uses, or the "
                             "store and the miss path will disagree. '0-20:1' renders one tile "
                             "per request, the way this script worked before metatiling.")
    parser.add_argument("--metatile-buffer", type=int, default=None,
                        help="pixels rendered around each block and then cropped away "
                             "(default: METATILE_BUFFER_PX from .env)")
    parser.add_argument("--log-every", type=int, default=250, help="progress line every N tiles")
    parser.add_argument("--max-disk-gb", type=float, default=20,
                        help="pause with a warning when less free space remains")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    parser.add_argument("--estimate", action="store_true", help="dry-run plus ETA at --tps")
    parser.add_argument("--tps", type=float, default=DEFAULT_TPS,
                        help="tiles/second assumed for --estimate (measured ~1.5)")
    parser.add_argument("--retry-failed", action="store_true", help="only re-attempt logged failures")
    parser.add_argument("--rebuild-index", action="store_true",
                        help="hash existing tiles into the dedup index before starting")
    parser.add_argument("--self-test", action="store_true", help="run internal tests and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_tests()

    env = load_env(args.env_file)

    # Renderer URL: the .env value is the compose-internal name on dev setups, useless to
    # a host-side script; anything else set there wins, else localhost on the published port.
    env_renderer = (env.get("RENDERER_URL") or "").strip()
    if args.renderer_url:
        renderer_url = args.renderer_url.rstrip("/")
    elif env_renderer and env_renderer != "http://renderer":
        renderer_url = env_renderer.rstrip("/")
    else:
        port = env.get("RENDERER_PUBLISH_PORT") or str(DEFAULT_RENDERER_PORT)
        renderer_url = f"http://127.0.0.1:{port}"

    out_root = args.out_dir or env.get("TILES_STORE_DIR") or "/mnt/meow/OSM/tiles"
    project_version = (env.get("PROJECT_VERSION") or "").strip()

    routes_raw = env.get("TILE_LAYER_ROUTES", "")
    try:
        routes = parse_routes(routes_raw)
    except ValueError as exc:
        sys.exit(f"pregenerate: {exc}")

    metatile_raw = args.metatile or env.get("METATILE_SIZE") or DEFAULT_METATILE
    try:
        metatile = parse_metatile(metatile_raw)
    except ValueError as exc:
        sys.exit(f"pregenerate: {exc}")
    metatile_buffer = args.metatile_buffer
    if metatile_buffer is None:
        metatile_buffer = int(env.get("METATILE_BUFFER_PX") or DEFAULT_METATILE_BUFFER)
    if metatile_buffer < 0:
        sys.exit("pregenerate: --metatile-buffer must not be negative")
    layers = (args.layers or ",".join(public_layers(routes, "countries"))).split(",")
    layers = [l.strip() for l in layers if l.strip()]
    for layer in layers:
        if not is_safe_layer_name(layer):
            sys.exit(f"pregenerate: layer {layer!r} is not a valid layer name")
    if not layers:
        sys.exit("pregenerate: no layers to pregenerate")

    if not (0 <= args.min_zoom <= args.max_zoom < 32):
        sys.exit(f"pregenerate: zoom range {args.min_zoom}-{args.max_zoom} is invalid")
    if args.concurrency < 1:
        sys.exit("pregenerate: --concurrency must be at least 1")

    total = sum(count_tiles(layers, z) for z in range(args.min_zoom, args.max_zoom + 1))
    print(f"renderer:        {renderer_url}")
    print(f"store:           {out_root}")
    print(f"layers:          {', '.join(layers)}")
    print(f"zoom range:      {args.min_zoom}..{args.max_zoom} ({total:,} tiles)")
    print(f"metatile:        {metatile_raw} +{metatile_buffer}px buffer")
    print("routes:")
    for public, lo, hi, upstream in routes:
        if public in layers:
            print(f"  {public}@{lo}-{hi} -> {upstream}")
    if not routes:
        print("  (none: every layer renders from its own name)")
    print()

    if args.dry_run or args.estimate:
        cum = 0
        for z in range(args.min_zoom, args.max_zoom + 1):
            n, secs = estimate_zoom(z, layers, args.tps)
            cum += n
            edge = metatile_size(metatile, z)
            renders = len(layers) * ((1 << z) // edge) ** 2
            if args.estimate:
                print(f"  z{z:<3} {n:>14,} tiles   cum {cum:>14,}   "
                      f"{renders:>12,} renders ({edge}x{edge})   ~{fmt_duration(secs)} alone")
            else:
                print(f"  z{z:<3} {n:>14,} tiles   {renders:>12,} renders ({edge}x{edge})")
        if args.estimate:
            total_secs = total / args.tps if args.tps else float("inf")
            print(f"  total               ~{fmt_duration(total_secs)} at {args.tps} tiles/s "
                  f"(measured renderer rate ~1-2 tiles/s)")
            print("  the ETA assumes one render per tile, so with metatiling it is an upper "
                  "bound:\n  the render column is what actually gets issued.")
        return 0

    # From here on we write. Fail fast if the store is not writable (fuseblk mounts are
    # picky about uids, and this script must run as the user that owns /mnt/meow).
    probe_write_access(out_root)
    if project_version:
        atomic_write(os.path.join(out_root, PROJECT_VERSION_MARKER), project_version.encode())
        print(f"project marker:  {PROJECT_VERSION_MARKER} -> {project_version[:12]}…")
    else:
        print("project marker:  PROJECT_VERSION not set; the API will wipe this store at boot")

    index = Index(os.path.join(out_root, INDEX_DB))
    try:
        if args.rebuild_index:
            rebuild_index(index, out_root, layers, args.min_zoom, args.max_zoom, print)

        failures_path = os.path.join(out_root, FAILURES_FILE)
        return _execute(
            renderer_url, out_root, layers, routes, index, failures_path,
            args.min_zoom, args.max_zoom, args.concurrency, args.timeout or
            float(env.get("RENDER_TIMEOUT_SECS") or 60), args.log_every,
            args.max_disk_gb, args.retry_failed, metatile, metatile_buffer,
        )
    finally:
        index.close()


def _execute(renderer_url, out_root, layers, routes, index, failures_path,
             min_zoom, max_zoom, concurrency, timeout, log_every, max_disk_gb,
             retry_failed, metatile, metatile_buffer):
    store = Store(out_root, index)

    def keep(layer, z, x, y, data, started):
        """Blank-collapse, dedup and store one tile; shared by both render paths."""
        sha = hashlib.sha256(data).hexdigest()
        kind, stored = store.put(layer, z, x, y, data, sha)
        return {"kind": kind, "layer": layer, "z": z, "x": x, "y": y,
                "ms": int((time.monotonic() - started) * 1000), "bytes": stored}

    def failed(layer, z, x, y, started, exc):
        return {"kind": "failed", "layer": layer, "z": z, "x": x, "y": y,
                "ms": int((time.monotonic() - started) * 1000), "bytes": 0,
                "error": str(exc)}

    def one_tile(layer, z, x, y):
        """A single tile, rendered on its own. Used wherever METATILE_SIZE leaves a zoom
        unbanded, and at z0/z1 where a block cannot fit in the pyramid."""
        started = time.monotonic()
        try:
            if valid_existing(store.target(layer, z, x, y)):
                return [{"kind": "skipped", "layer": layer, "z": z, "x": x, "y": y,
                         "ms": 0, "bytes": 0}]
            upstream = resolve_upstream(routes, layer, z)
            data = fetch_with_retry(renderer_url, upstream, z, x, y, timeout)
            return [keep(layer, z, x, y, data, started)]
        except TileError as exc:
            return [failed(layer, z, x, y, started, exc)]

    def one_block(layer, z, mx, my, n):
        """A whole n x n block in one render, then sliced. Returns one result per tile."""
        if n <= 1:
            return one_tile(layer, z, mx, my)

        started = time.monotonic()
        coords = [(mx + dx, my + dy) for dy in range(n) for dx in range(n)]
        # Resume is per block: a partially written block would have to be re-rendered anyway,
        # and re-slicing the whole thing is free next to the render.
        if all(valid_existing(store.target(layer, z, x, y)) for x, y in coords):
            return [{"kind": "skipped", "layer": layer, "z": z, "x": x, "y": y,
                     "ms": 0, "bytes": 0} for x, y in coords]

        upstream = resolve_upstream(routes, layer, z)
        try:
            data = fetch_metatile_with_retry(
                renderer_url, upstream, z, mx, my, n, metatile_buffer, timeout
            )
            sliced = list(slice_metatile(data, n, metatile_buffer))
        except TileError as exc:
            # The block is one unit of work, so it fails as one: every tile in it is logged, and
            # --retry-failed will bring the whole block back.
            return [failed(layer, z, x, y, started, exc) for x, y in coords]

        # The render cost is one number for the whole block; charging it to the first tile and
        # nothing to the rest keeps the rate and ETA arithmetic in the progress line honest.
        results = []
        for dx, dy, tile in sliced:
            results.append(keep(layer, z, mx + dx, my + dy, tile, started))
            started = time.monotonic()
        return results

    blocks = None
    if retry_failed:
        logged = []
        try:
            with open(failures_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    logged.append((entry["layer"], entry["z"], entry["x"], entry["y"]))
        except FileNotFoundError:
            pass
        # A failure is logged per tile but rendered per block, so several logged tiles usually
        # collapse into one retry. Order-preserving dedup.
        blocks = list(dict.fromkeys(
            (layer, z, x - x % (n := metatile_size(metatile, z)), y - y % n, n)
            for layer, z, x, y in logged
        ))
        print(f"retrying {len(logged)} logged failure(s) in {len(blocks)} block(s)")
        if not blocks:
            return 0

    def work_items():
        if blocks is not None:
            yield from blocks
            return
        for z in range(min_zoom, max_zoom + 1):
            yield from iter_blocks(layers, z, metatile_size(metatile, z))

    def remaining_items():
        if blocks is not None:
            return sum(n * n for *_, n in blocks)
        return sum(count_tiles(layers, z) for z in range(min_zoom, max_zoom + 1))

    counters = {"rendered": 0, "dup": 0, "blank": 0, "skipped": 0, "failed": 0}
    processed = 0
    logged_at = 0
    ema_ms = 0.0
    last_disk_check = 0.0
    start = time.monotonic()
    retry_failures = []  # still failing, rewritten at the end in retry mode

    def log_progress(force=False):
        # `processed` advances by a whole block at a time now, so it will rarely land exactly on a
        # multiple of log_every — hence a watermark rather than a modulo.
        nonlocal ema_ms, logged_at
        if not force and processed // log_every == logged_at:
            return
        logged_at = processed // log_every
        elapsed = time.monotonic() - start
        rate = processed / elapsed if elapsed > 0 else 0
        eta_s = (remaining_items() - processed) / rate if rate > 0 else float("inf")
        print(
            f"[{time.strftime('%H:%M:%S')}] {processed:,}/{remaining_items():,} "
            f"({100.0 * processed / max(remaining_items(), 1):.3f}%) "
            f"rendered={counters['rendered']:,} dup={counters['dup']:,} "
            f"blank={counters['blank']:,} skipped={counters['skipped']:,} "
            f"failed={counters['failed']:,} "
            f"rate={rate:.2f}/s avg={ema_ms:.0f}ms ETA~{fmt_duration(eta_s)}",
            flush=True,
        )

    def check_disk():
        nonlocal last_disk_check
        now = time.monotonic()
        if now - last_disk_check < 60:
            return
        last_disk_check = now
        try:
            vfs = os.statvfs(out_root)
            free_gb = vfs.f_bavail * vfs.f_frsize / 1e9
            if free_gb < max_disk_gb:
                print(f"WARNING: only {free_gb:.1f} GB free on {out_root} "
                      f"(limit {max_disk_gb} GB); pausing 60s", flush=True)
                time.sleep(60)
        except OSError:
            pass

    failures_lock = threading.Lock()

    def record_failure(t):
        with failures_lock:
            with open(failures_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({**t, "ts": int(time.time())}) + "\n")
            if blocks is not None:
                retry_failures.append(t)

    def bump(kind):
        if kind == "rendered":
            counters["rendered"] += 1
        elif kind.startswith("dup"):
            counters["dup"] += 1
        elif kind.startswith("blank"):
            counters["blank"] += 1
        elif kind == "skipped":
            counters["skipped"] += 1
        elif kind == "failed":
            counters["failed"] += 1

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = pool.map(lambda t: one_block(*t), work_items())
            # One work item is a block, so it comes back as a list of per-tile results. Everything
            # downstream — counters, the failure log, the ETA — still counts in tiles.
            for results in futures:
                for result in results:
                    processed += 1
                    if result["ms"] > 0:
                        ema_ms = ema_ms * 0.95 + result["ms"] * 0.05 if ema_ms else result["ms"]
                    bump(result["kind"])
                    if result["kind"] == "failed":
                        record_failure(result)
                check_disk()
                log_progress()
    except KeyboardInterrupt:
        print("\ninterrupted; shutting down workers…", flush=True)
        log_progress(force=True)
        _write_summary(counters, failures_path, start)
        return 130

    log_progress(force=True)
    _write_summary(counters, failures_path, start)

    if blocks is not None:
        tmp = failures_path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            for t in retry_failures:
                f.write(json.dumps({**t, "ts": int(time.time())}) + "\n")
        os.replace(tmp, failures_path)
        print(f"failures file rewritten: {len(retry_failures)} still failing")
    return 0


def _write_summary(counters, failures_path, start):
    elapsed = time.monotonic() - start
    total_done = sum(counters.values())
    print(
        f"\nsummary: {total_done:,} tiles in {fmt_duration(elapsed)} — "
        f"rendered={counters['rendered']:,} deduped={counters['dup']:,} "
        f"blank={counters['blank']:,} skipped={counters['skipped']:,} "
        f"failed={counters['failed']:,}",
        flush=True,
    )
    if counters["failed"]:
        print(f"failures logged to {failures_path}; re-run with --retry-failed to retry")


# ---------------------------------------------------------------------------------------
# Self-tests


def make_raw_png(width, height, rows):
    """Wraps pre-filtered scanlines (`<filter byte><pixels>` per row) into a valid PNG."""

    def chunk(ctype, payload):
        return (struct.pack(">I", len(payload)) + ctype + payload
                + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_MAGIC + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def make_png(width, height, rgba, filter_type=0):
    """Tiny uniform PNG builder (8-bit RGBA, one chosen scanline filter) for decoder tests."""
    stride = width * 4
    rows = b""
    prev = bytearray()
    for row in range(height):
        raw = bytearray(bytes(rgba) * width)
        line = bytearray(raw)
        if filter_type == 1:  # Sub: encode as difference from the left neighbour
            for i in range(4, stride):
                line[i] = (raw[i] - raw[i - 4]) & 0xFF
        elif filter_type == 2 and prev:  # Up: encode as difference from the row above
            for i in range(stride):
                line[i] = (raw[i] - prev[i]) & 0xFF
        rows += bytes([filter_type]) + bytes(line)
        prev = raw
    return make_raw_png(width, height, rows)


def run_self_tests():
    failures = []

    def check(label, cond):
        print(f"  {'PASS' if cond else 'FAIL'}: {label}")
        if not cond:
            failures.append(label)

    print("routes (mirrors src/config.rs tests):")
    r = parse_routes("countries@0-5=simple-countries")
    check("band inclusive at both ends", resolve_upstream(r, "countries", 0) == "simple-countries"
          and resolve_upstream(r, "countries", 5) == "simple-countries")
    check("past the band falls through", resolve_upstream(r, "countries", 6) == "countries")
    check("unrouted layer untouched", resolve_upstream(r, "roads", 3) == "roads")
    r2 = parse_routes("countries@0-5=simple-countries,countries@6-20=countries-detail")
    check("two-band routing", resolve_upstream(r2, "countries", 6) == "countries-detail")
    r3 = parse_routes("  countries@0-5 = simple-countries , ")
    check("whitespace tolerated", resolve_upstream(r3, "countries", 2) == "simple-countries")
    for bad in ["countries@0-5", "countries=simple-countries", "countries@0=simple",
                "countries@5-0=simple", "countries@0-999=simple", "countries@0-5=../etc",
                "a/b@0-5=simple", "countries@0-5="]:
        try:
            parse_routes(bad)
            check(f"malformed route rejected: {bad!r}", False)
        except ValueError:
            check(f"malformed route rejected: {bad!r}", True)

    print("metatile bands (mirrors src/config.rs tests):")
    m = parse_metatile("0-6:2,7-20:4")
    check("band inclusive at both ends", metatile_size(m, 0) == 1 and metatile_size(m, 6) == 2)
    check("clamped to the pyramid at z0/z1", metatile_size(m, 1) == 2 and metatile_size(m, 0) == 1)
    check("second band applies", metatile_size(m, 7) == 4 and metatile_size(m, 20) == 4)
    check("unbanded zoom renders one tile", metatile_size(m, 21) == 1)
    check("empty spec disables metatiling", metatile_size(parse_metatile(""), 9) == 1)
    for bad in ["0-6", "0-6:", "6:2", "6-0:2", "0-99:2", "0-6:3", "0-6:0", "0-6:16", "0-6:two"]:
        try:
            parse_metatile(bad)
            check(f"malformed band rejected: {bad!r}", False)
        except ValueError:
            check(f"malformed band rejected: {bad!r}", True)

    print("metatile geometry (mirrors src/metatile.rs tests):")
    world = metatile_bbox(0, 0, 0, 1, 0)
    check("z0 unbuffered block is the whole world",
          all(abs(abs(v) - MERCATOR_HALF_SPAN) < 1e-6 for v in world))
    top = metatile_bbox(2, 0, 0, 1, 0)
    bottom = metatile_bbox(2, 0, 3, 1, 0)
    check("row 0 is the northern edge", abs(top[3] - MERCATOR_HALF_SPAN) < 1e-6
          and abs(bottom[1] + MERCATOR_HALF_SPAN) < 1e-6 and top[1] > bottom[3])
    span = 2 * MERCATOR_HALF_SPAN / 8
    plain, padded = metatile_bbox(3, 4, 4, 2, 0), metatile_bbox(3, 4, 4, 2, TILE_PX)
    check("a one-tile buffer grows the extent symmetrically",
          all(abs(padded[i] - (plain[i] - span)) < 1e-6 for i in (0, 1))
          and all(abs(padded[i] - (plain[i] + span)) < 1e-6 for i in (2, 3)))
    # A 2x2 block of unbuffered single tiles must tile the same ground as one 2x2 block.
    block = metatile_bbox(5, 8, 8, 2, 0)
    corners = [metatile_bbox(5, 8 + dx, 8 + dy, 1, 0) for dx in (0, 1) for dy in (0, 1)]
    check("a block covers exactly the tiles it contains",
          abs(min(c[0] for c in corners) - block[0]) < 1e-6
          and abs(max(c[2] for c in corners) - block[2]) < 1e-6
          and abs(min(c[1] for c in corners) - block[1]) < 1e-6
          and abs(max(c[3] for c in corners) - block[3]) < 1e-6)

    print("layer-name safety:")
    for good in ["countries", "simple-countries", "dark-basemap_v2.1"]:
        check(f"{good!r} accepted", is_safe_layer_name(good))
    for bad in ["", "..", ".", "../../etc", "a/b", "a b", "layer\0"]:
        check(f"{bad!r} rejected", not is_safe_layer_name(bad))

    print("uniform-PNG detection:")
    solid = make_png(8, 8, (10, 20, 30, 255))
    check("uniform RGBA detected", png_is_uniform(solid))
    solid_sub = make_png(8, 8, (10, 20, 30, 255), filter_type=1)
    check("uniform with Sub filter detected", png_is_uniform(solid_sub))
    solid_up = make_png(2, 8, (1, 2, 3, 255), filter_type=2)
    check("uniform with Up filter detected", png_is_uniform(solid_up))
    two_tone_rows = (b"\x00" + bytes((10, 20, 30, 255)) + bytes((99, 99, 99, 255))) * 2  # 2 rows x 2 px
    two_tone = make_raw_png(2, 2, two_tone_rows)
    check("two-tone PNG not uniform", not png_is_uniform(two_tone))
    check("non-PNG not uniform", not png_is_uniform(b"garbage data"))
    check("garbage PNG not uniform", not png_is_uniform(PNG_MAGIC + b"garbage"))

    print("store behaviour (temp dir):")
    with tempfile.TemporaryDirectory(prefix="pregenerate-test-") as tmp:
        out = os.path.join(tmp, "tiles")
        idx = Index(os.path.join(out, INDEX_DB))
        store = Store(out, idx)
        a = store.target("countries", 1, 0, 0)
        b = store.target("countries", 1, 1, 0)
        # Non-uniform on purpose: a uniform payload takes the blank path, not the dedup path.
        payload = make_raw_png(2, 2, two_tone_rows)
        kind1, _ = store.put("countries", 1, 0, 0, payload, hashlib.sha256(payload).hexdigest())
        kind2, _ = store.put("countries", 1, 1, 0, payload, hashlib.sha256(payload).hexdigest())
        check("identical content hardlinked", kind1 == "rendered" and kind2.startswith("dup-")
              and os.path.samefile(a, b))
        blank_payload = make_png(4, 4, (0, 0, 0, 255))
        kind3, _ = store.put("countries", 1, 1, 1, blank_payload,
                             hashlib.sha256(blank_payload).hexdigest())
        check("uniform tile collapses to canonical blank",
              kind3.startswith("blank-")
              and os.path.samefile(store.target("countries", 1, 1, 1),
                                   blank_canonical(out, "countries", 1)))
        check("no temp files left behind",
              not [n for n in os.listdir(os.path.dirname(a)) if n.endswith(".tmp")])
        check("dedup index persisted", idx.find("countries", 1,
              hashlib.sha256(payload).hexdigest()) is not None)
        idx.close()

    print("marker + probe (temp dir):")
    with tempfile.TemporaryDirectory(prefix="pregenerate-test-") as tmp:
        atomic_write(os.path.join(tmp, PROJECT_VERSION_MARKER), b"v1")
        with open(os.path.join(tmp, PROJECT_VERSION_MARKER)) as f:
            check("marker written atomically", f.read() == "v1")
        probe_write_access(tmp)
        check("write probe passes on writable dir", True)

    if failures:
        print(f"\n{len(failures)} self-test(s) FAILED")
        return 1
    print("\nall self-tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
