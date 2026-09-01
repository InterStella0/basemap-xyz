# basemap-xyz

A free, no-API-key dark basemap. OpenStreetMap data with custom QGIS symbology, rendered by QGIS
Server and served as plain XYZ tiles.

```
https://basemap.queeniemella.cc/tiles/{layer}/{z}/{x}/{y}.png
```

e.g for layer "countries"
```
https://basemap.queeniemella.cc/tiles/countries/{z}/{x}/{y}.png
```


## How it fits together

```
Internet
  └─ reverse (nginx :80)
       ├─ /                                → the single-page site
       ├─ /tiles/{layer}/{z}/{x}/{y}.png   → try_files on the shared cache volume
       │                                       HIT  → sendfile; Rust is never involved
       │                                       MISS → @render
       └─ @render                          → api:3000 (Poem)
                                                └─ HTTP → renderer:80/ows/  (QGIS Server WMTS)
                                                             └─ PostGIS (external)
```

Three containers:

| Service | What it does |
|---|---|
| `reverse` | nginx. Serves the site, answers cache hits straight off disk, forwards only misses. |
| `api` | The Rust/Poem tile cache. Renders a missing tile once, stores it, hands it back. |
| `renderer` | QGIS Server. The slow, expensive part; everything else exists to call it rarely. |


### Why nginx serves the hits

A tile that is already on disk should not cost a proxy hop and a userspace copy. nginx maps the URL
directly onto the cache volume with `try_files` and `sendfile`s the file; the API only ever sees a
miss. That makes the concurrency limit for cached tiles "whatever nginx can do", and leaves the Rust
service with one job.

The layout `{z}/{x}/{y}.png` is therefore a contract between two files:
`cache_path()` in `src/tiles.rs` and the `try_files` line in `nginx/nginx.conf`. Change one without
the other and every hit silently becomes a miss.

### Why the API is more than a proxy

QGIS Server renders a tile in the hundreds of milliseconds and runs a small fixed pool of FCGI
processes. A cold popular tile can therefore be requested by hundreds of clients before the first
render finishes. The API handles that with:

- **Single-flight** (`moka::try_get_with`) — N concurrent requests for one tile produce exactly one
  render. There is a test pinning this (`concurrent_requests_for_one_tile_collapse_into_a_single_render`).
- **A semaphore** capped at `RENDER_CONCURRENCY`, kept at or below the renderer's
  `FCGID_MAX_PROCESSES`. Queueing beyond that just moves the backlog inside QGIS, where it cannot be
  timed out. Waiting too long returns `503` + `Retry-After`.
- **A negative cache**, so a permanently broken tile fails once a minute rather than once a request.
- **Atomic writes** (`.tmp` + `rename`), because nginx is reading the same directory and must never
  see a half-written PNG.

## Setup

1. **Add your project.** Drop `project.qgz` into `qgis-server/project/` — see the README there for
   the two settings QGIS Desktop needs (publish the layer for WMTS, tick EPSG:3857). Whenever you
   replace it later, run `scripts/sync-project-version.sh` and then
   `docker compose up -d --build renderer`. The `--build` matters: `qgis-server/Dockerfile` *copies*
   the project into the image rather than mounting it, so a plain `up -d` flushes the tile cache and
   then refills it from the old cartography still baked into the running image.
2. **Configure.** `cp default.env .env` and fill in `BASEMAP_PUBLIC_URL` and the `DB_*` values.
3. **Run.** `docker compose up -d --build`
4. **(Optional) Add OSM detail.** `scripts/build-osm-detail.sh` then
   `scripts/patch-project.py --detail` — see [Zoom-tiered detail](#zoom-tiered-detail).

The site and tiles are then on `${EXPOSE_PORT}` (8080 by default).

Database credentials never enter the project file or the image: the layers reference
`service=mellabasemap`, and `qgis-server/entrypoint.sh` writes the matching `pg_service.conf` and
`.pgpass` at container start.

## Swarm deployment

`compose.swarm.yaml` splits the stack across two machines: the **renderer** runs on the box that
holds the OSM detail GeoPackages (they do not fit on the server), while **api + reverse** run on
the VPS, which stays the public entrypoint and the swarm manager.

```
Internet
  └─ VPS (manager, basemap.role=frontend)
       ├─ reverse (nginx :80, published :8080 mode=host)
       └─ api ── RENDERER_URL=http://<home-box-tailscale-ip>:8081 ──┐
                                                                    │ Tailscale (not the overlay)
  home box (worker, basemap.role=renderer)                          │
       └─ renderer (QGIS, published :8081 mode=host) ◄─────────────┘
              ├─ /mnt/meow/OSM/out → /data (44 GB of GeoPackages)
              └─ Postgres at DB_HOST (home LAN)
```

The api→renderer hop deliberately bypasses the swarm overlay network (its VXLAN cannot traverse a
home NAT) and goes over Tailscale through a host-published port instead.

**One-time setup:**

1. Join the home box to the existing swarm (a node can only be in one swarm; leave any old one
   first with `docker swarm leave --force`):
   ```sh
   # on the VPS:
   docker swarm join-token worker
   # on the home box:
   docker swarm join --token <token> <vps-tailscale-ip>:2377
   ```
2. Label the nodes on the VPS — placement constraints match these labels, not hostnames:
   ```sh
   docker node update --label-add basemap.role=frontend <vps-node>
   docker node update --label-add basemap.role=renderer <home-node>
   ```
3. Make sure the Tailscale ACL lets the VPS reach the home box on tcp/8081 (or whatever
   `RENDERER_PUBLISH_PORT` is).

**Every deploy:**

1. Push the images (`docker stack deploy` does not build): `scripts/push-swarm-images.sh`.
2. Set `SWARM_RENDERER_URL=http://<home-box-tailscale-ip>:8081` in `.env`, copy that `.env` next
   to `compose.swarm.yaml` on the VPS (`env_file` and `${...}` interpolation are both read from
   the manager at deploy time), then:
   ```sh
   docker stack deploy -c compose.swarm.yaml dark-basemap
   ```

Notes:

- `tile_cache` stays a node-local volume — fine, because api and reverse are both pinned to the
  VPS node and the renderer never touches it. `PROJECT_VERSION` cache flushing works unchanged.
- Both published ports use `mode: host` + a placement constraint, so they bind only on their
  intended node and nginx sees real client IPs (its rate limits depend on that).
- Cross-node overlay/VXLAN errors on the renderer node are benign: no traffic uses the overlay
  between the two nodes.
- `DB_HOST` must stay routable from the renderer node (today: a home-LAN address, unchanged).

### When the renderer is down

A circuit breaker in the API counts consecutive transport-level failures (`RENDER_FAILURE_THRESHOLD`,
default 3). Once tripped it fails every render attempt fast for `RENDER_CIRCUIT_OPEN_SECS` (default
30) instead of letting each uncached tile burn the full `RENDER_TIMEOUT_SECS`. Any response from the
renderer — even an HTTP error — resets it.

While the renderer is unreachable, an uncached tile answers **503 + `Retry-After: 30` +
`no-store`**, and the negative cache is deliberately skipped for that class so every miss keeps
answering 503 rather than mixing in 502s. Cached tiles keep serving as normal nginx hits, `/health`
reports `degraded`, and `/stats` gains `circuit_opens` and `renderer_unavailable` counters. A tile
the renderer *answered* badly (bad layer, service exception) remains a negative-cached 502.

## Zoom-tiered detail

One public URL, two different QGIS layers behind it. Zoomed out, tiles come from `simple-countries`
— 258 Natural Earth polygons in PostGIS, cheap to draw at continental scale. Zoomed in, they come
from `countries`, a group of OpenStreetMap layers in GeoPackages. Clients never see the seam.

`TILE_LAYER_ROUTES` in `.env` is the switch:

```
TILE_LAYER_ROUTES=countries@0-5=simple-countries
```

Comma-separated `<public>@<lo>-<hi>=<upstream>`; first match wins, and any layer or zoom without a
rule renders from its own name. So the line above sends z0–5 to `simple-countries` and leaves z6–20
on `countries`. A malformed rule is a boot panic, not a silent fallback.

The tile is always cached under the **public** name, so `/tiles/countries/9/271/171.png` lives at
`countries/9/271/171.png` on the volume no matter which layer drew it — nginx's `try_files` knows
nothing about routing and does not need to.

### Building the detail store

QGIS can open a `.osm.pbf`, but it cannot serve tiles from one: GDAL's OSM driver has no spatial
index, so a bbox query decompresses and scans the whole file. Measured on this data, 32 MB takes
1.4 s and 752 MB takes 38 s — linear, which puts a 33 GB extract at ~28 minutes for a single tile.
A GeoPackage is also just a file, but it carries an R-tree, so the same query is milliseconds.

```sh
scripts/build-osm-detail.sh              # .osm.pbf -> GeoPackages (hours; resumable)
python3 scripts/patch-project.py --detail  # add the layers to project.qgz
scripts/sync-project-version.sh
docker compose up -d --build renderer
```

The script reads every `*.osm.pbf` in `/mnt/meow/OSM` and writes one GeoPackage per theme into
`$OSM_DETAIL_DIR`, which `compose.yaml` mounts read-only at `/data` in the renderer. It records
finished `(theme, region)` pairs, so an interrupted run resumes instead of restarting.

Raw OSM will not fit a sane disk budget — full-resolution planet geometry is ~358 GB, half of it
building footprints. The default profile therefore filters by class and simplifies geometry to a
tolerance that is sub-pixel at the zoom each layer draws at, an **18×** reduction on a test extract
(4.1 GB → 224 MB).

Measured on the full planet (all 8 Geofabrik continent extracts, 79 GB of `.osm.pbf`, ~14 h):

| Theme | Features | Size |
|---|---|---|
| `landuse` | 73,362,104 | 30 GB |
| `water` | 23,699,230 | 8.5 GB |
| `roads` | ~29,000,000 | 6.0 GB |
| `countries` (unused, see below) | 375 | 31 MB |
| **total** | | **44.5 GB** |

A tile-sized bbox query against the 30 GB `landuse` file returns in **~40 ms**. The same query
against the raw `.osm.pbf` would take ~28 minutes; that difference is the entire reason this
conversion exists.

| Variable | |
|---|---|
| `REGIONS` | Only build these extracts (substring match). Empty = all. |
| `BUILDING_REGIONS` | Build building footprints for these extracts only. Empty = skip buildings entirely. Planet-wide they are ~179 GB on their own, at ~250 bytes per footprint, and they only render at z15+. |
| `CPL_TMPDIR` | **Must have tens of GB free.** GDAL's OSM driver spills a temp SQLite DB here; if it runs out of space the driver fails *silently* and writes a valid but empty GeoPackage. Defaults to `/mnt/meow/OSM/tmp`, deliberately not `/tmp` (a 7.4 GB tmpfs on this host). |

### Why the land base is Natural Earth, not OSM

The detail group draws OSM water, landuse and roads on top of a land polygon that comes from the
*PostGIS Natural Earth table* — the same data `simple-countries` uses — rather than from OSM
`admin_level=2` boundaries. That is not for lack of trying: the planet build did produce a
`countries.gpkg` from OSM, and it is unusable as a base. GDAL's OSM driver cannot reliably assemble
the largest boundary relations, and gives up with `Non closed ring detected`. Measured on the
finished build, 4 of 12 sample cities had no land polygon at all — Paris, Madrid, New York and
Moscow, i.e. France, Spain, the USA and Russia. Those are exactly the countries with scattered
overseas territories, or (Russia) an antimeridian crossing.

Natural Earth is generalized, so coastlines are coarser at z14+ than OSM would be. It is also
complete, and for the layer everything else is stacked on, complete beats precise. `countries.gpkg`
is still built and left in place; nothing references it.

`patch-project.py` skips any theme whose GeoPackage is absent, so a run without buildings produces a
working project rather than a broken one — QGIS Server runs with `QGIS_SERVER_IGNORE_BAD_LAYERS=0`,
where one missing file would otherwise take down the whole project.

## Cache behaviour

Tiles live for `TILE_TTL_DAYS` (120). A janitor sweep runs at boot and every
`TILE_SWEEP_INTERVAL_HOURS` (6), unlinking expired tiles and orphaned temp files and pruning the
directories that empties.

**nginx does not check the TTL** — it serves whatever is on disk. So a tile can survive up to
`TTL + sweep_interval`. That is intended for a basemap; it is not a bug.

If `PROJECT_VERSION` is set in `.env`, the API compares it against a marker it keeps at the root of
the tile cache volume on every boot; a mismatch — including "no marker yet" — wipes the whole cache
before serving anything. `scripts/sync-project-version.sh` sets `PROJECT_VERSION` to a hash of
`qgis-server/project/project.qgz`, so updating the cartography is: replace the file, run the script,
`docker compose up -d --build renderer`. Leave `PROJECT_VERSION` empty to opt out and keep pure
TTL-only behaviour.

Note that `TILE_LAYER_ROUTES` changes tile *content* without changing `project.qgz`, so the hash
does not move and the cache is **not** flushed automatically. Use the manual flush below after
editing routes.

For a flush not tied to `project.qgz` (e.g. a database-only or routing change), fall back to the
manual flush:

```sh
docker compose stop api reverse
docker run --rm -v dark-basemap-xyz_tile_cache:/c alpine sh -c 'rm -rf /c/*'
docker compose start api reverse
```

## Endpoints

| Route | |
|---|---|
| `GET /tiles/{layer}/{z}/{x}/{y}.png` | The basemap. `.png` optional, `{layer}` any WMTS-published layer short name. Out-of-range or unsafe layer name → `404`. |
| `GET /health` | Always `200`; body reports whether the renderer is reachable. |
| `GET /health/live` | Liveness only, touches nothing downstream. What the container healthcheck polls. |
| `GET /stats` | Counters: renders, errors, queue timeouts, avg render ms, cache size. |

`/health` stays `200` with the renderer down on purpose — nginx is still serving hits perfectly
well, so flipping the probe would take a working container out of rotation over a dependency it
cannot fix. Read the body, not the status.

`/stats` only counts what reached the API. Since nginx answers hits, `disk_hits` is *not* your hit
rate — it is the narrow case of a tile that landed between nginx's `stat()` and ours. Use nginx's
logs for the real ratio.

## Development

```sh
cargo test                  # unit + route tests, no containers needed
cargo run                   # needs RENDERER_URL and TILE_CACHE_DIR set
docker build --target test .   # runs the suite inside the image build (CI gate)
```

The route tests point the renderer at a closed port, so anything that returns `502` provably passed
validation and anything that returns `404` provably did not reach the renderer.

## Licenses

Two different things, two different licenses:

- **This code** (the tile cache, the QGIS Server setup, the site) is [MIT](LICENSE). Fork it, run
  your own, do what you like.
- **The map data** is OpenStreetMap, licensed [ODbL](https://opendatacommons.org/licenses/odbl/).
  MIT on the code does not put the tiles in the public domain; if you serve them, you carry the
  attribution requirement below.

## Attribution

The cartography is mine; the geometry is OpenStreetMap, licensed
[ODbL](https://opendatacommons.org/licenses/odbl/). Rendered tiles are a Produced Work, so the
credit has to be visible wherever the map is

```html
&copy; <a href="https://basemap.queeniemella.cc">queeniemella</a>
&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors
```

Every snippet on the site already includes it.
