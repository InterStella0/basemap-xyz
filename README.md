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

A tile that is already on disk should not cost a proxy hop and a userspace copy. On a single
host, nginx maps the URL directly onto the tile directory with `try_files` and `sendfile`s the
file; the API only ever sees a miss. That makes the concurrency limit for cached tiles "whatever
nginx can do", and leaves the Rust service with one job.

The layout `{z}/{x}/{y}.png` is therefore a contract between two files:
`cache_path()` in `src/tiles.rs` and the `try_files` line in `nginx/nginx.conf`. Change one without
the other and every hit silently becomes a miss.

> In the **swarm** deployment (below) the reverse proxy runs on the VPS while the tile store and
> the API live on the home box, so nginx has no directory to `try_files` and proxies every tile
> to the API over Tailscale. The same `nginx.conf` serves both topologies: without the mount,
> `try_files` falls through to the API for every request.

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
4. **(Optional) Add OSM detail.** `scripts/load-natural-earth-states.sh`, then
   `scripts/build-osm-detail.sh`, then `scripts/patch-project.py --base --detail` — see
   [Zoom-tiered detail](#zoom-tiered-detail) and [Labels](#labels).

The site and tiles are then on `${EXPOSE_PORT}` (8080 by default).

Database credentials never enter the project file or the image: the layers reference
`service=mellabasemap`, and `qgis-server/entrypoint.sh` writes the matching `pg_service.conf` and
`.pgpass` at container start.

## Swarm deployment

`compose.swarm.yaml` splits the stack across two machines: the **renderer** and the **api** run on
the home box, which holds the OSM detail GeoPackages, the SSD tile store and Postgres — while
**reverse** alone runs on the VPS, which stays the public entrypoint and the swarm manager. The api
moved home so it can read and write the pregenerated tile store on the SSD directly; swarm volumes
cannot span nodes, so there is no other way for it to touch that disk.

```
Internet
  └─ VPS (manager, basemap.role=frontend)
       └─ reverse (nginx :80, published :8080 mode=host)
              └─ API_HOST=<home-box-tailscale-ip>, API_PORT=3045 ──┐
                                                                │ Tailscale (not the overlay)
  home box (worker, basemap.role=renderer)                      │
       ├─ api (published :3045 → container :3000 mode=host) ◄─────────────────────┘
       │     ├─ /mnt/meow/OSM/tiles → /var/cache/tiles (SSD tile store, rw)
       │     └─ RENDERER_URL=http://renderer (overlay, same node)
       └─ renderer (QGIS, published :8081 mode=host)
              ├─ /mnt/meow/OSM/out → /data (44 GB of GeoPackages)
              └─ Postgres at DB_HOST (home LAN)
```

The reverse→api hop bypasses the swarm overlay network (its VXLAN cannot traverse a home NAT) and
goes over Tailscale through a host-published port, exactly as the old api→renderer hop did. Every
tile request — hits included — crosses that link, so the home uplink is now the serving bottleneck;
that is the accepted tradeoff for keeping the tiles on the SSD. `scripts/pregenerate-tiles.py`
(run on the home box) fills the store ahead of time so the api rarely has to render.

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
3. Make sure the Tailscale ACL lets the VPS reach the home box on tcp/3045 (`API_PUBLISH_PORT`)
   for the api; tcp/8081 (`RENDERER_PUBLISH_PORT`) is only needed if you also want to reach the
   renderer directly from the VPS.
4. `/mnt/meow` is a fuseblk (NTFS via FUSE) mount owned by uid 1000, so the api service runs as
   `user: "1000:1000"` — the image's own uid 10001 cannot write that mount. If the tiles store
   moves to a normal filesystem, drop the `user:` override.

**Every deploy:**

1. Push the images (`docker stack deploy` does not build): `scripts/push-swarm-images.sh`.
2. Set `SWARM_API_HOST=<home-box-tailscale-ip>` in `.env`, copy that `.env` next to
   `compose.swarm.yaml` on the VPS (`env_file` and `${...}` interpolation are both read from
   the manager at deploy time), then:
   ```sh
   docker stack deploy -c compose.swarm.yaml dark-basemap
   ```

The API handles Docker's SIGTERM and drains requests already in flight for up to 70 seconds before
exiting. Its Compose `stop_grace_period` is 75 seconds so Swarm does not SIGKILL a live tile render
and turn it into an nginx `upstream prematurely closed connection` 502. Keep that drain window
longer than `RENDER_TIMEOUT_SECS` if the render timeout is changed. With one API replica on a
host-published port this protects accepted requests; it does not provide a second backend for new
connections during the short replacement gap.

Notes:

- There is no `tile_cache` volume any more: the api serves the SSD store via a bind mount, and
  both the api and the renderer are pinned to the home box, which is the only node that has that
  disk.
- Both published ports use `mode: host` + a placement constraint, so they bind only on their
  intended node and nginx sees real client IPs (its rate limits depend on that).
- Cross-node overlay/VXLAN errors on the renderer node are benign: no tile traffic uses the
  overlay between the two nodes (api→renderer is same-node; reverse→api is Tailscale).
- `DB_HOST` must stay routable from the renderer node (today: a home-LAN address, unchanged).
- Migration from the old layout: after deploying and confirming tiles serve, remove the stale
  node-local volume on the VPS with `docker volume rm dark-basemap_tile_cache`.

## Pregenerating tiles

`scripts/pregenerate-tiles.py` renders whole zoom ranges ahead of time into
`$TILES_STORE_DIR` (`/mnt/meow/OSM/tiles`), the same directory the api serves from, so static
data is never re-rendered on demand. Run it on the home box, next to the renderer:

```sh
scripts/pregenerate-tiles.py --estimate --max-zoom 8   # counts + ETA, renders nothing
scripts/pregenerate-tiles.py --max-zoom 15             # the real thing, z0..15
scripts/pregenerate-tiles.py --retry-failed            # re-attempt logged failures
```

It is built for very long, interruptible runs:

- **Resumable** — tiles already on disk are skipped; Ctrl-C any time and re-run.
- **Atomic** — writes go to a sibling `.tmp` then rename, exactly like the API, so a
  half-written PNG is never servable.
- **Deduplicating** — byte-identical renders become hardlinks to one canonical file per
  (layer, zoom), and all-same-pixel tiles (empty ocean, empty land) collapse to a canonical
  blank. Storage cost tracks *distinct* content, not tile count.
- **Failure-tolerant** — one bad tile never stops the run; failures are logged to
  `.pregenerate-failures.jsonl` in the store and retried with `--retry-failed`.
- **Route-aware** — it parses `TILE_LAYER_ROUTES` from `.env` with the same semantics as the
  API, so z0–3 renders `simple-countries` and z4+ renders `countries` automatically.
- **Metatiling, the same way the API does** — it reads `METATILE_SIZE` and `METATILE_BUFFER_PX`
  from the same `.env` and renders a block per request, so the store and the miss path produce the
  same labels. This is not optional: a store built one tile at a time serves clipped labels no
  matter what the API does. The slicing needs Pillow; `--metatile 1` is the old path and stays pure
  stdlib. `--estimate` prints renders as well as tiles, which is now the number that matters.
- **Honest about scale** — see the table below.

It also writes the `.project-version` marker into the store, so the API's boot-time version
sync sees a match instead of wiping the pregenerated tiles.

### How long this takes

Measured on the live renderer: one warm tile takes 0.6–2.8 s, and concurrent renders mostly
serialize (5 in parallel ≈ 1.1 tiles/s). Metatiling divides the *render* count by up to 16 while
leaving the tile count alone — at z7 it turned 8.9 s/tile into 2.9 s/tile — so read the table below
as an upper bound. The cumulative tile counts are unforgiving either way:

| Range | Tiles | At ~1.5 tiles/s |
|---|---|---|
| z0–8 | 87,381 | ~16 h |
| z0–10 | 1,398,101 | ~11 days |
| z0–11 | 5,592,405 | ~43 days |
| z0–12 | 22,369,621 | ~6 months |
| z0–13 | 89,478,485 | ~2 years |
| z0–14 | 357,913,941 | ~7.5 years |
| z0–15 | 1,431,655,765 | ~30 years |

So a full-planet z0–15 is a background job that runs for as long as it runs; start low and let
it climb. The dedup means most of those tiles cost one render *and one directory entry*, which is
the other hard limit: a billion files need roughly a terabyte of filesystem metadata alone. The
script pauses with a warning below `--max-disk-gb` free space rather than filling the disk
silently, so check `df /mnt/meow` before asking for z13+.

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
TILE_LAYER_ROUTES=countries@0-3=simple-countries
```

Comma-separated `<public>@<lo>-<hi>=<upstream>`; first match wins, and any layer or zoom without a
rule renders from its own name. So the line above sends z0–3 to `simple-countries` and leaves z4–20
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
scripts/load-natural-earth-states.sh          # Natural Earth admin-1 -> PostGIS public.states
scripts/build-osm-detail.sh                   # .osm.pbf -> GeoPackages (hours; resumable)
python3 scripts/patch-project.py --base --detail   # add the layers and labels to project.qgz
scripts/sync-project-version.sh
docker compose up -d --build renderer
```

`patch-project.py --detail` refuses to write the project if a PostGIS table a layer needs is
missing, because a bad PostGIS layer under `QGIS_SERVER_IGNORE_BAD_LAYERS=0` fails the whole project
at load time — every tile 500s, with the reason only in the renderer's log.

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
| `places` | ~1,700,000 | ~250 MB |
| `countries` (unused, see below) | 375 | 31 MB |
| **total** | | **~44.8 GB** |

`places` is the outlier in that table: it took **minutes**, not hours. It comes from the `points`
layer, which is plain tagged nodes, so GDAL never builds the node-reference index that dominates
every other pass. Measured on the 752 MB central-america extract, a full scan of `points` is 4.2 s
against 64 s for the same file's `multipolygons`.

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

## Labels

Text comes from two sources, chosen per label rather than per zoom.

**Names of places that have a fixed identity** — countries and states — come from Natural Earth in
PostGIS. That is the same data the land base is drawn from, so the label agrees with the shape under
it, and because it is a small indexed table it can be queried at *every* zoom, including z0–3 where
the OSM detail group is not even loaded. `simple-countries` therefore carries country labels of its
own: the low-zoom layer is not a fallback that loses the names, it is where the names come from.

**Names of things you only care about up close** — cities, towns, villages, lakes and bays — come
from OpenStreetMap, and switch on as you zoom in.

| Layer | Source | Zooms | Notes |
|---|---|---|---|
| `simple-countries` | PostGIS `countries` | z0–3 | The low-zoom route; carries the same country labels as below |
| `country-labels` | PostGIS `countries` | z4–9 | Uppercase, letter-spaced |
| `states` | PostGIS `states` | z4+ | Dashed `#666` boundary line, no label — brighter than a major road, because past z11 it is the only cue for which state you are in |
| `state-labels-major` | PostGIS `states` | z5–11 | `labelrank <= 4` — the ~800 units Natural Earth ranks worth a world-map name |
| `state-labels` | PostGIS `states` | z7–11 | `labelrank` 5–19; 20 is Natural Earth's "never label this" bucket |
| `places-metro` | `places.gpkg` | z4+ | `place=city`, population ≥ 1M — 652 worldwide |
| `places-city` | `places.gpkg` | z6+ | population 200k–1M |
| `places-city-minor` | `places.gpkg` | z8+ | the remaining cities, including the 1,819 with no population tag |
| `places-town` | `places.gpkg` | z9+ | `town`, `borough`; smaller dot |
| `places-village` | `places.gpkg` | z12+ | `village`, `suburb`; name only, no dot |
| `water-labels` | `water.gpkg` | z6+ | Italic, `name IS NOT NULL` |

Cities are tiered because `place=city` alone is 11,906 points and all of them on one z5 tile buries
the continent. OSM's `population` is free text and often absent, so it is `CAST` and `coalesce`d to
0: a city with no population lands in the bottom tier, which is the safe direction to be wrong in.
Country and state labels stop at z9 and z11 — a country name is orientation at z6 and noise at z12,
where you can already see the streets it is sitting on.

Three mechanisms do the work, and all three matter:

- **`centroidWhole="1"`** on every polygon label. Without it QGIS anchors a polygon's label at the
  centroid of the part visible in the current request extent, so "France" would be redrawn in every
  block France touches. With it the anchor is the whole geometry's centroid and the name is drawn
  once — which also means it lands in whichever block holds the country's centre, so from about z10
  a state name is present but sparse. That is why the state label band stops at z11.
- **`minFeatureSize`**, in mm. A country too small to be worth 6 mm on screen loses its label until
  you zoom in far enough. This stages the whole set by zoom for free, so `country-labels` is one
  layer rather than one per importance tier.
- **A datasource subset per layer.** `places-city` and `places-town` read the same GeoPackage
  through different `subset=` filters, so each slice gets its own `min_z`. The filter runs in the
  GeoPackage's own SQLite (or in PostGIS), so it costs an index lookup, not a scan.

Place labels prefer the `name:en` tag where mappers supplied one and fall back to the local-script
`name`. The renderer image installs DejaVu and Noto (including CJK) for the rest; without them every
non-Latin name renders as a row of tofu boxes.

### The seam, and why tiles are rendered in blocks

QGIS labels each request against the extent it is given and nothing else. Ask it for one 256×256
tile and a label whose text crosses that tile's edge is cut in half, and two labels in adjacent
tiles cannot see each other, so they overprint. A 4×4 block of z7 tiles over Germany fetched one at
a time gives `…lefeld`, `Nüremb…`, `Karlsr…`, and *Frankfurt* printed straight through *Wiesbaden*.

So the API does not render tiles. It renders **metatiles**: an `n × n` block in one WMS `GetMap`,
plus `METATILE_BUFFER_PX` of overspill on every side, then cuts the result into 256 px tiles and
writes all `n²` of them. See `src/metatile.rs`.

- Inside a block there are no seams at all: every label in it was placed against one canvas.
- Across block boundaries the **buffer** is what stitches: a label is centred on its anchor, so an
  anchor up to `METATILE_BUFFER_PX` outside the block is still drawn here, at exactly the position
  the neighbouring block draws it. The default 128 px is a little over half the width of the widest
  label this cartography produces.
- What can still go wrong: a label near a block edge whose collision outcome differs between the two
  renders. Rare, and one block boundary in four is the worst case rather than every tile edge.

It is also **cheaper**, because a QGIS request carries a large fixed cost regardless of size —
roughly 7 s at z7 on this hardware. Measured against the live renderer:

| | one at a time | as one 4×4 block (+128 px) |
|---|---|---|
| z7 Germany, 16 tiles | 142.3 s | 45.8 s |
| z9 Germany, 16 tiles | 72 s | 51.1 s |
| z12 Munich, 16 tiles | 1.6 s | 0.9 s |

`METATILE_SIZE` is banded by zoom (`0-6:2,7-20:4`) because that fixed cost is not the whole story:
`water` and `landuse` are drawn from the full OSM extracts from z4 up, so a *single* z5 tile over
Europe is 23 s and a 4×4 block there would run past 100 s.

Two consequences worth knowing:

- **`scripts/pregenerate-tiles.py` metatiles too**, reading the same two keys from `.env`. It has to:
  the pregenerated store is what the site actually serves, so a store built one tile at a time would
  serve broken labels no matter what the API does. Its slicing needs Pillow; `--metatile 1` is the
  old tile-at-a-time path and stays pure stdlib.
- **A block is a bigger unit of work than a tile**, so `RENDER_TIMEOUT_SECS` is 240 rather than 60,
  and the renderer image raises its own nginx `fastcgi_read_timeout` to match
  (`qgis-server/qgis-timeouts.conf`). The stock image caps every render at 60 s and — because its
  `error_page` target does not exist — answers an overrun with a **404**, which the API cannot tell
  apart from a genuinely bad tile.

### What is not labelled

Road names. `scripts/build-osm-detail.sh` extracted `roads.gpkg` with only the `highway` column, so
adding them means re-running the whole 6 GB `lines` pass with `name` and `ref` — hours, and a job of
its own.

## Cache behaviour

Tiles live for `TILE_TTL_DAYS` — 3650 on the durable store: it is a long-term archive, not a
scratch cache, and sweeping pregenerated tiles would mean re-rendering them. A janitor sweep runs
at boot and every `TILE_SWEEP_INTERVAL_HOURS` (24 — the store holds millions of files and the
walk is not free), unlinking expired tiles and orphaned temp files and pruning the directories
that empties.

**nginx does not check the TTL** — it serves whatever is on disk. So a tile can survive up to
`TTL + sweep_interval`. That is intended for a basemap; it is not a bug.

**One store, two stacks.** On the home box the compose stack and the Swarm stack both bind-mount
`$TILES_STORE_DIR`. That is fine while they render the same `project.qgz`, and a trap while they do
not: the compose api wipes the whole store at boot whenever `PROJECT_VERSION` changes, which throws
away the tiles the Swarm stack is serving to the public — and the Swarm api has no `PROJECT_VERSION`
of its own, so it will never wipe or notice. When iterating on cartography ahead of a deploy, point
the local stack somewhere else:

```sh
TILES_STORE_DIR=/mnt/meow/OSM/tiles-dev docker compose up -d
```

and put it back once both stacks are on the same image.

If `PROJECT_VERSION` is set in `.env`, the API compares it against a marker it keeps at the root of
the tile store on every boot; a mismatch — including "no marker yet" — wipes the whole store
before serving anything. `scripts/sync-project-version.sh` sets `PROJECT_VERSION` to a hash of
`qgis-server/project/project.qgz`, so updating the cartography is: replace the file, run the script,
`docker compose up -d --build renderer`, then re-run `scripts/pregenerate-tiles.py` (the wipe made
every z0–15 tile a miss again, which is exactly right — the cartography changed). Leave
`PROJECT_VERSION` empty to opt out and keep pure TTL-only behaviour.

Note that `TILE_LAYER_ROUTES` changes tile *content* without changing `project.qgz`, so the hash
does not move and the cache is **not** flushed automatically. Use the manual flush below after
editing routes.

For a flush not tied to `project.qgz` (e.g. a database-only or routing change), fall back to the
manual flush (the store is a bind mount now, not a named volume):

```sh
docker compose stop api reverse
rm -rf "${TILES_STORE_DIR:-/mnt/meow/OSM/tiles}"/*
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

The cartography is mine. The detail geometry and every place, town and water body name are
OpenStreetMap, licensed [ODbL](https://opendatacommons.org/licenses/odbl/); the land base, the
state boundaries and the country and state names are [Natural Earth](https://www.naturalearthdata.com/),
which is public domain and asks for no credit. Rendered tiles are a Produced Work, so the OSM credit
has to be visible wherever the map is

```html
&copy; <a href="https://basemap.queeniemella.cc">queeniemella</a>
&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors
```

Every snippet on the site already includes it.
