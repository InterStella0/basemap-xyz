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
| `simple-countries` | 258 Natural Earth Admin-0 polygons. Cheap at continental scale. | PostGIS `public.countries` via `service=mellabasemap` |
| `countries` | A **group** of OSM layers — country polygons, water, landuse, roads, optionally buildings. | GeoPackages at `/data/*.gpkg` |

Publishing a *group* rather than a layer is what lets several styled layers answer to one name.
Each layer inside the group carries its own scale-based visibility, so QGIS does not query the
buildings GeoPackage at z8 at all.

## Editing the cartography

`project.qgz` is a zip, so git sees one opaque binary blob. `scripts/patch-project.py` is the
reviewable source of truth instead — it is idempotent and re-applies the same intent:

```sh
python3 scripts/patch-project.py --base     # rename the PostGIS layer, fix the WMTS pyramid depth
python3 scripts/patch-project.py --detail   # add the OSM group (needs the GeoPackages to exist)
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
4. **Detail layer paths.** The OSM layers reference `/data/<theme>.gpkg`, which is
   `${OSM_DETAIL_DIR}` bind-mounted read-only by `compose.yaml`. The container path is contractual —
   changing it invalidates the project file.
5. **QGIS version.** This project was saved by QGIS **4.2.1**, so `QGIS_VERSION` is pinned to
   `4.2.1`. Keep the two in step when you upgrade the desktop.

Keep credentials out of the project file: reference the connection as `service=<name>` with no host,
user or password inline, and let the entrypoint supply the rest from the `DB_*` variables.
