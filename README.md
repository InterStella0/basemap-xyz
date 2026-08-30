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
   replace it later, run `scripts/sync-project-version.sh` and then `docker compose up -d` — that
   flushes the tile cache immediately instead of waiting out `TILE_TTL_DAYS`.
2. **Configure.** `cp default.env .env` and fill in `BASEMAP_PUBLIC_URL` and the `DB_*` values.
3. **Run.** `docker compose up -d --build`

The site and tiles are then on `${EXPOSE_PORT}` (8080 by default).

Database credentials never enter the project file or the image: the layers reference
`service=mellabasemap`, and `qgis-server/entrypoint.sh` writes the matching `pg_service.conf` and
`.pgpass` at container start.

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
`docker compose up -d`. Only `api`'s config changed, so compose recreates just that container — no
image rebuild needed. Leave `PROJECT_VERSION` empty to opt out and keep pure TTL-only behaviour.

For a flush not tied to `project.qgz` (e.g. a database-only change), fall back to the manual flush:

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
