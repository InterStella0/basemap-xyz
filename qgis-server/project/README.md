# `project.qgz`

The image expects `qgis-server/project/project.qgz`. It is **copied into the renderer image**, not
mounted — so after any change you need `docker compose up -d --build renderer`, not just `up -d`.
A plain `up -d` flushes the tile cache and then refills it from the old cartography still baked into
the running image, which looks exactly like the change silently not working.

## What the project publishes

Two WMTS names, both served through the single public URL `/tiles/countries/{z}/{x}/{y}.png`; the
API picks between them per zoom via `TILE_LAYER_ROUTES` (see the main README).

| WMTS name | What it is | Source |
|---|---|---|
| `simple-countries` | Natural Earth land, ocean, country names and low-zoom marine names. | PostGIS `public.countries`, `public.ocean`, `public.marine_areas` via `service=mellabasemap` |
| `countries` | The same Natural Earth base plus states and OSM water, landuse, roads, places, and optional buildings. | PostGIS plus GeoPackages at `/data/*.gpkg` |

Publishing a *group* rather than a layer is what lets several styled layers answer to one name.
Each layer inside the group carries its own scale-based visibility, so QGIS does not query the
buildings GeoPackage at z8 at all.

## Editing the cartography

`project.qgz` is a zip, so git sees one opaque binary blob. `scripts/patch-project.py` is the
reviewable source of truth instead — it is idempotent and re-applies the same intent:

```sh
scripts/load-natural-earth-ocean.sh         # first run: create ocean + marine label tables
python3 scripts/patch-project.py --base     # build the low-zoom group, fix the WMTS pyramid depth
python3 scripts/patch-project.py --detail   # add the detailed group (needs its data to exist)
```

Editing in QGIS Desktop is still fine; re-run the script afterwards to re-assert the parts it owns.
It keeps the previous file at `project.qgz.bak`.

## Settings that must stay in step

1. **CRS.** Publish EPSG:3857 under **Project → Properties → QGIS Server → WMTS** — that is what
   creates the tile matrix set the API asks for by name.
2. **Pyramid depth.** `WMTSGrids/Config` carries a level count and `WMTSMinScale` truncates it
   further. These were 18 levels and `5004`, which capped the service at z16 while `.env` advertised
   `MAX_ZOOM=20`, so z17–20 returned `ServiceException`. `--base` sets 21 levels and `WMTSMinScale=0`.
   If you change `MAX_ZOOM`, change these too.
3. **Database service name.** The PostGIS layer references `service=mellabasemap`; `PG_SERVICE_NAME`
   in `.env` must match exactly. `entrypoint.sh` writes the matching `pg_service.conf` at start.
4. **Natural Earth tables.** Both groups require `public.countries`, `public.ocean` and
   `public.marine_areas`; the detail group additionally requires `public.states`. Load the ocean
   tables with `scripts/load-natural-earth-ocean.sh` before running `--base`.
5. **Detail layer paths.** The OSM layers reference `/data/<theme>.gpkg`, which is
   `${OSM_DETAIL_DIR}` bind-mounted read-only by `compose.yaml`. The container path is contractual —
   changing it invalidates the project file.
6. **QGIS version.** This project was saved by QGIS **4.2.1**, so `QGIS_VERSION` is pinned to
   `4.2.1`. Keep the two in step when you upgrade the desktop.

Keep credentials out of the project file: reference the connection as `service=<name>` with no host,
user or password inline, and let the entrypoint supply the rest from the `DB_*` variables.
