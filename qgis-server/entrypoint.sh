#!/bin/bash
set -e

[[ $DEBUG == "1" ]] && env

# QGIS Server runs as www-data, so the credentials have to be readable by that user and nobody
# else. The .qgz stores its PostGIS layers as a `service=...` reference carrying no password, which
# is what keeps database credentials out of the project file and out of the image.
cat > /var/www/.pgpass <<PGPASS
${DB_HOST}:${DB_PORT}:${DB_NAME}:${DB_USERNAME}:${DB_PASSWORD}
PGPASS
chmod 600 /var/www/.pgpass
chown www-data:www-data /var/www/.pgpass

# Standard system location, so libpq finds it without a PGSERVICEFILE override. The service name
# must match the `service=` in the .qgz's layer datasources exactly, or the layers fail to load
# with no error visible in a GetTile response.
PG_SERVICE_NAME=${PG_SERVICE_NAME:-mellabasemap}
mkdir -p /etc/postgresql-common
cat > /etc/postgresql-common/pg_service.conf <<SERVICE
[${PG_SERVICE_NAME}]
host=${DB_HOST}
port=${DB_PORT}
dbname=${DB_NAME}
user=${DB_USERNAME}
SERVICE

exec "$@"
