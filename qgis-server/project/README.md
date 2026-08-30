# Drop `project.qgz` here

The image expects `qgis-server/project/project.qgz`. It is gitignored — the cartography is yours to
version however you like, but a `docker compose build` will fail without it.

## What the current project needs before it will serve tiles

Checked against the `project.qgz` in this directory:

1. **WMTS is not published yet.** The project contains no WMTS configuration at all, so `GetTile`
   returns a `ServerException` rather than a PNG. In QGIS Desktop go to
   **Project → Properties → QGIS Server → WMTS**, add the layer or group you want served, and tick
   **EPSG:3857**. That is what creates the tile matrix set the API asks for by name.
2. **Layer name.** The project currently publishes one layer, `countries`, but the API has no
   fixed idea of which layer to serve — every WMTS-published layer's short name is reachable
   directly at `/tiles/{layer}/{z}/{x}/{y}.png`. Publish as many layers as you like under
   **QGIS Server → WMTS** and each becomes servable without touching `.env`.
3. **Database service name.** The layer datasource references `service=mellabasemap`.
   `PG_SERVICE_NAME` in `.env` must match it exactly; `entrypoint.sh` writes the matching
   `pg_service.conf` at container start.
4. **Credentials.** `DB_USERNAME` and `DB_PASSWORD` are empty in `default.env`. Until they are set,
   the renderer logs `fe_sendauth: no password supplied` and every layer loads as invalid.
5. **QGIS version.** This project was saved by QGIS **4.2.1**, so `QGIS_VERSION` is pinned to
   `4.2.1`. Loading it under an older image (`ltr` was 3.44) makes QGIS warn
   "Problems may occur" — keep the two in step when you upgrade the desktop.

Keep credentials out of the project file: reference the connection as `service=<name>` with no
host, user or password inline, and let the entrypoint supply the rest from the `DB_*` variables.
