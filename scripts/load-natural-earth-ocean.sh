#!/usr/bin/env bash
# Load Natural Earth's ocean surface and marine label areas into PostGIS.
#
# The ocean source is a single, very detailed MultiPolygon. Leaving it that way defeats the GiST
# index: every tile intersects the one world-sized row and QGIS has to fetch all ~450k vertices.
# Split it into small, outline-free polygon pieces so a tile only reads nearby geometry. The style
# in patch-project.py draws a same-colour hairline around each piece to hide subdivision seams.
#
# Imports land in staging tables first. The published tables are replaced only after both downloads
# imported successfully, so a network or GDAL failure leaves the currently served data untouched.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OCEAN_URL="${NE_OCEAN_URL:-https://naciscdn.org/naturalearth/10m/physical/ne_10m_ocean.zip}"
MARINE_URL="${NE_MARINE_AREAS_URL:-https://naciscdn.org/naturalearth/10m/physical/ne_10m_geography_marine_polys.zip}"
OCEAN_TABLE="${NE_OCEAN_TABLE:-ocean}"
MARINE_TABLE="${NE_MARINE_AREAS_TABLE:-marine_areas}"
MAX_VERTICES="${NE_OCEAN_MAX_VERTICES:-256}"

die() { printf 'load-natural-earth-ocean: %s\n' "$*" >&2; exit 1; }
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

[ -f "$REPO/.env" ] || die "$REPO/.env not found (cp default.env .env)"

# These values become quoted SQL identifiers below. Keep the override useful without letting an
# accidental shell fragment become SQL.
for ident in "$OCEAN_TABLE" "$MARINE_TABLE"; do
    [[ "$ident" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "invalid table name: $ident"
done
[[ "$MAX_VERTICES" =~ ^[0-9]+$ ]] && [ "$MAX_VERTICES" -ge 5 ] \
    || die "NE_OCEAN_MAX_VERTICES must be an integer >= 5"

# Read only the database keys. Sourcing .env is unsafe here because other values contain spaces.
env_get() { sed -n "s/^$1=//p" "$REPO/.env" | tail -1; }
for v in DB_HOST DB_PORT DB_NAME DB_USERNAME DB_PASSWORD; do
    printf -v "$v" '%s' "$(env_get "$v")"
    [ -n "${!v:-}" ] || die "$v is not set in .env"
done

for cmd in curl unzip ogr2ogr psql; do
    command -v "$cmd" >/dev/null || die "$cmd not on PATH"
done

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

download_shape() {
    local url="$1" stem="$2" dest="$3"
    log "downloading $(basename "$url")"
    curl -fsSL "$url" -o "$tmp/$stem.zip" || die "download failed: $url"
    unzip -q -o "$tmp/$stem.zip" -d "$tmp/$stem"
    local shape
    shape="$(find "$tmp/$stem" -name "$stem.shp" -print -quit)"
    [ -n "$shape" ] || die "$stem.shp not found in downloaded archive"
    printf -v "$dest" '%s' "$shape"
}

download_shape "$OCEAN_URL" ne_10m_ocean ocean_shape
download_shape "$MARINE_URL" ne_10m_geography_marine_polys marine_shape

pg=(psql -v ON_ERROR_STOP=1 -qtAX -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USERNAME" -d "$DB_NAME")
pg_dsn="PG:host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USERNAME"
ocean_stage="${OCEAN_TABLE}_import"
marine_stage="${MARINE_TABLE}_import"

log "importing ocean staging geometry"
PGPASSWORD="$DB_PASSWORD" ogr2ogr -f PostgreSQL "$pg_dsn" "$ocean_shape" \
    -nln "$ocean_stage" -nlt MULTIPOLYGON -overwrite \
    -lco GEOMETRY_NAME=geom -lco FID=fid -lco SPATIAL_INDEX=GIST \
    -t_srs EPSG:4326

log "importing marine label areas"
PGPASSWORD="$DB_PASSWORD" ogr2ogr -f PostgreSQL "$pg_dsn" "$marine_shape" \
    -nln "$marine_stage" -nlt PROMOTE_TO_MULTI -overwrite \
    -lco GEOMETRY_NAME=geom -lco FID=fid -lco SPATIAL_INDEX=GIST \
    -t_srs EPSG:4326

ocean_source_n="$(PGPASSWORD="$DB_PASSWORD" "${pg[@]}" \
    -c "select count(*) from public.\"$ocean_stage\"")"
marine_source_n="$(PGPASSWORD="$DB_PASSWORD" "${pg[@]}" \
    -c "select count(*) from public.\"$marine_stage\"")"
[ "$ocean_source_n" -gt 0 ] || die "downloaded ocean dataset imported 0 rows"
[ "$marine_source_n" -gt 0 ] || die "downloaded marine dataset imported 0 rows"

log "subdividing ocean geometry and publishing both tables"
PGPASSWORD="$DB_PASSWORD" "${pg[@]}" <<SQL
BEGIN;

DROP TABLE IF EXISTS public."$OCEAN_TABLE";
CREATE TABLE public."$OCEAN_TABLE" AS
SELECT
    row_number() OVER ()::bigint AS fid,
    source.featurecla,
    source.scalerank,
    source.min_zoom,
    part.geom::geometry(Polygon, 4326) AS geom
FROM public."$ocean_stage" AS source
CROSS JOIN LATERAL ST_Dump(
    ST_CollectionExtract(ST_MakeValid(source.geom), 3)
) AS dumped
CROSS JOIN LATERAL ST_Subdivide(dumped.geom, $MAX_VERTICES, 0.0) AS part(geom)
WHERE NOT ST_IsEmpty(part.geom);
ALTER TABLE public."$OCEAN_TABLE" ADD PRIMARY KEY (fid);
CREATE INDEX "${OCEAN_TABLE}_geom_gix" ON public."$OCEAN_TABLE" USING GIST (geom);
ANALYZE public."$OCEAN_TABLE";

DROP TABLE IF EXISTS public."$MARINE_TABLE";
CREATE TABLE public."$MARINE_TABLE" AS
SELECT
    row_number() OVER ()::bigint AS fid,
    source.featurecla,
    source.name,
    source.name_en,
    source.label,
    source.min_label,
    source.max_label,
    source.scalerank,
    source.ne_id,
    ST_Multi(ST_CollectionExtract(ST_MakeValid(source.geom), 3))
        ::geometry(MultiPolygon, 4326) AS geom
FROM public."$marine_stage" AS source
WHERE NOT ST_IsEmpty(source.geom);
ALTER TABLE public."$MARINE_TABLE" ADD PRIMARY KEY (fid);
CREATE INDEX "${MARINE_TABLE}_geom_gix" ON public."$MARINE_TABLE" USING GIST (geom);
ANALYZE public."$MARINE_TABLE";

DROP TABLE public."$ocean_stage";
DROP TABLE public."$marine_stage";
COMMIT;
SQL

IFS='|' read -r ocean_n ocean_max ocean_invalid ocean_srids <<EOF
$(PGPASSWORD="$DB_PASSWORD" "${pg[@]}" -F '|' -c \
    "select count(*), max(ST_NPoints(geom)), count(*) filter (where not ST_IsValid(geom)), string_agg(distinct ST_SRID(geom)::text, ',') from public.\"$OCEAN_TABLE\"")
EOF
IFS='|' read -r marine_n marine_invalid marine_srids <<EOF
$(PGPASSWORD="$DB_PASSWORD" "${pg[@]}" -F '|' -c \
    "select count(*), count(*) filter (where not ST_IsValid(geom)), string_agg(distinct ST_SRID(geom)::text, ',') from public.\"$MARINE_TABLE\"")
EOF

[ "$ocean_n" -gt 0 ] || die "public.$OCEAN_TABLE has no rows after subdivision"
[ "$ocean_max" -le "$MAX_VERTICES" ] || die "public.$OCEAN_TABLE subdivision exceeded $MAX_VERTICES vertices"
[ "$ocean_invalid" -eq 0 ] || die "public.$OCEAN_TABLE contains invalid geometry"
[ "$ocean_srids" = 4326 ] || die "public.$OCEAN_TABLE has unexpected SRID(s): $ocean_srids"
[ "$marine_n" -gt 0 ] || die "public.$MARINE_TABLE has no rows"
[ "$marine_invalid" -eq 0 ] || die "public.$MARINE_TABLE contains invalid geometry"
[ "$marine_srids" = 4326 ] || die "public.$MARINE_TABLE has unexpected SRID(s): $marine_srids"

for table in "$OCEAN_TABLE" "$MARINE_TABLE"; do
    indexes="$(PGPASSWORD="$DB_PASSWORD" "${pg[@]}" -c \
        "select count(*) from pg_indexes where schemaname='public' and tablename='$table' and indexdef ilike '%gist%'")"
    [ "$indexes" -gt 0 ] || die "public.$table has no GiST index"
done

log "public.$OCEAN_TABLE: $ocean_n pieces, <= $ocean_max vertices each, spatial index ok"
log "public.$MARINE_TABLE: $marine_n label areas, spatial index ok"
echo
echo "next: python3 scripts/patch-project.py --base --detail"
