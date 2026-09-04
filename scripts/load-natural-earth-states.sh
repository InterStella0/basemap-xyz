#!/usr/bin/env bash
# Load Natural Earth admin-1 (states/provinces) into PostGIS as public.states.
#
# Why Natural Earth and not OSM: OSM's admin_level=4 boundaries live in the `multipolygons` layer,
# which means a full node-index pass over every .osm.pbf — measured at 64 s for a 752 MB extract,
# so ~2 h for the corpus — and scripts/build-osm-detail.sh already documents GDAL failing to
# assemble the largest boundary relations. Natural Earth is generalized but complete, and complete
# is what a boundary line needs to be. It is also the same source `land-base` and `simple-countries`
# already read, so the two agree with each other at the coastline.
#
# Credentials come from .env and stay there: project.qgz references `service=mellabasemap`, which
# qgis-server/entrypoint.sh resolves into pg_service.conf at container start.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
URL="${NE_STATES_URL:-https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip}"
TABLE="${NE_STATES_TABLE:-states}"

die() { printf 'load-natural-earth-states: %s\n' "$*" >&2; exit 1; }
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

[ -f "$REPO/.env" ] || die "$REPO/.env not found (cp default.env .env)"

# Read the five keys we need rather than sourcing the file: .env holds unquoted values with spaces
# in them (TRUSTED_PROXY_CIDR is a space-separated CIDR list), and `. .env` tries to execute the
# second word as a command.
env_get() { sed -n "s/^$1=//p" "$REPO/.env" | tail -1; }

for v in DB_HOST DB_PORT DB_NAME DB_USERNAME DB_PASSWORD; do
    printf -v "$v" '%s' "$(env_get "$v")"
    [ -n "${!v:-}" ] || die "$v is not set in .env"
done

command -v ogr2ogr >/dev/null || die "ogr2ogr not on PATH (install gdal)"
command -v unzip >/dev/null || die "unzip not on PATH"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

log "downloading $(basename "$URL")"
curl -fsSL "$URL" -o "$tmp/ne.zip" || die "download failed: $URL"
unzip -q -o "$tmp/ne.zip" -d "$tmp/ne"

shp="$(find "$tmp/ne" -name '*.shp' | head -1)"
[ -n "$shp" ] || die "no .shp inside the archive"

# -overwrite so re-running is idempotent. MULTIPOLYGON because a handful of admin-1 units are
# islands; a mixed geometry column makes QGIS_SERVER_TRUST_LAYER_METADATA guess wrong.
log "loading $(basename "$shp") -> public.$TABLE"
PGPASSWORD="$DB_PASSWORD" ogr2ogr -f PostgreSQL \
    "PG:host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USERNAME" \
    "$shp" \
    -nln "$TABLE" -nlt MULTIPOLYGON -overwrite \
    -lco GEOMETRY_NAME=geom -lco FID=fid -lco SPATIAL_INDEX=GIST \
    -t_srs EPSG:4326

n=$(PGPASSWORD="$DB_PASSWORD" psql -qtAX -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USERNAME" -d "$DB_NAME" \
        -c "select count(*) from public.\"$TABLE\"")
[ "$n" -gt 0 ] || die "public.$TABLE loaded 0 rows"

idx=$(PGPASSWORD="$DB_PASSWORD" psql -qtAX -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USERNAME" -d "$DB_NAME" \
        -c "select count(*) from pg_indexes where schemaname='public' and tablename='$TABLE' and indexdef ilike '%gist%'")
[ "$idx" -gt 0 ] || die "public.$TABLE has no GiST index; every tile would sequential-scan"

log "public.$TABLE: $n rows, spatial index ok"
echo
echo "if needed: scripts/load-natural-earth-ocean.sh"
echo "next: python3 scripts/patch-project.py --base --detail"
