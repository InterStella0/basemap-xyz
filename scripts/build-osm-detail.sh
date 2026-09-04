#!/usr/bin/env bash
# Build the OSM detail GeoPackages that back the `countries` WMTS group.
#
# Why a conversion is needed at all: QGIS can open a .osm.pbf, but GDAL's OSM driver has no spatial
# index, so a bbox query decompresses and scans the whole file rather than seeking. Measured on this
# data: 32 MB -> 1.4 s, 752 MB -> 38 s, linear. europe-latest.osm.pbf (33 GB) would therefore cost
# ~28 minutes for a single tile, against a 60 s RENDER_TIMEOUT_SECS. A GeoPackage is also just a
# file, but it carries an R-tree, so the same query is milliseconds. That index is the whole point.
#
# ---------------------------------------------------------------------------------------------
# Structure: two source passes, then a cheap split.
#
# The expensive part of reading a .osm.pbf is not decompression, it is resolving node references:
# GDAL builds a temporary SQLite index of every node, tens of GB per invocation. Measured mid-build,
# the workers had written ~130 GB to that temp DB while reading ~1 GB of actual input, with the disk
# 71% busy and the CPU 61% idle. The job is disk-bound on temp writes, which has two consequences:
#
#   1. Running themes concurrently does not help. Four workers ran 3.6x slower each, for ~10% more
#      aggregate throughput. Do not "parallelise" this by theme.
#   2. What matters is the NUMBER OF PASSES. countries, water and landuse all read `multipolygons`,
#      so extracting them separately rebuilt that index three times over the same data.
#
# So: one `multipolygons` pass writes a staging file with the union of what those three themes need,
# one `lines` pass produces roads directly, and the three polygon themes are then split out of the
# staging file with plain SQL — no OSM driver, no node index, minutes rather than hours.
#
# OSM_COMPRESS_NODES trades CPU (idle) for temp-DB size (the bottleneck), typically 30-50% smaller.
#
# Runs unattended for hours. Safe to interrupt and re-run: completed (pass, region) pairs are
# recorded in a state file and skipped.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${OSM_SRC_DIR:-/mnt/meow/OSM}"
OUT="${OSM_DETAIL_DIR:-$SRC/out}"
STATE="$OUT/.build-state"
STAGE="$OUT/_poly.gpkg"

# GDAL's OSM driver spills its node index here. It must have tens of GB free — europe alone built a
# 50 GB temp DB. Leaving this on /tmp (a 7.4 GB tmpfs on this host) makes the driver fail *silently*,
# producing a valid but empty GeoPackage.
export CPL_TMPDIR="${CPL_TMPDIR:-$SRC/tmp}"
export GDAL_CACHEMAX="${GDAL_CACHEMAX:-1024}"
export OSM_COMPRESS_NODES="${OSM_COMPRESS_NODES:-YES}"
export OGR_SQLITE_SYNCHRONOUS=OFF

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'build-osm-detail: %s\n' "$*" >&2; exit 1; }

# --- filters ----------------------------------------------------------------------------------
# The "planet under 50 GB" profile. Raw OSM geometry does not fit that budget in any uncompressed
# format, so both levers are pulled:
#
#   class filtering   minor roads are 88% of highway features (2.29M -> 171K on central-america)
#                     and only become legible at z16+.
#   simplification    0.0001 deg is ~11 m, sub-pixel at the zooms these layers draw at. Roads get a
#                     finer 0.00005 (~5.5 m) because they stay on screen to z20.
#
# Measured on central-america: 4.1 GB unfiltered -> 224 MB with this profile, an 18x reduction.
F_COUNTRIES="boundary='administrative' AND admin_level='2'"
F_WATER="\"natural\" IN ('water','bay','strait') OR landuse IN ('reservoir','basin')"
F_LANDUSE="landuse IN ('forest','residential','industrial','commercial','farmland','meadow','grass','cemetery','quarry') OR leisure IN ('park','golf_course','nature_reserve') OR \"natural\" IN ('wood','scrub','sand','heath','grassland','glacier','wetland')"
F_ROADS="highway IN ('motorway','trunk','primary','secondary','tertiary','motorway_link','trunk_link','primary_link','secondary_link')"

# Places are the label source, and they are cheap in a way no other theme is: they come from the
# `points` layer, which is plain tagged nodes, so GDAL never builds the node-reference index that
# makes the polygon passes cost hours. Measured: a full points scan of the 752 MB central-america
# extract is 4.2 s, against 64 s for the same file's multipolygons. The whole corpus took 8m32s
# and produced 1,924,480 points in a 220 MB GeoPackage.
#
# hamlet/isolated_dwelling/locality are excluded deliberately: they are the bulk of place=* nodes
# and are illegible clutter even at z16 on a basemap this dark.
F_PLACES="place IN ('city','town','village','suburb','borough')"
# name_en, not "name:en": scripts/osmconf-places.ini asks for the `name:en` tag, and GDAL already
# sanitizes the colon out of the field name it exposes. Selecting the raw tag name fails with
# "Unrecognized field name name:en". The label expression prefers name_en over the local-script
# name where a translation exists. population comes through as a string — OSM does not promise a
# number there — and patch-project.py tiers the city layers on CAST(population AS INTEGER) with a
# coalesce, so a missing or non-numeric value lands in the smallest tier rather than off the map.
C_PLACES="name,name_en,place,population"

# The staging pass keeps anything any of the three polygon themes will later claim.
F_POLY="($F_COUNTRIES) OR ($F_WATER) OR ($F_LANDUSE)"
C_POLY="boundary,admin_level,name,natural,landuse,leisure"

# pass | source layer | columns | simplify | filter | output
PASSES="poly|multipolygons|$C_POLY|0.0001|$F_POLY|$STAGE
roads|lines|highway|0.00005|$F_ROADS|$OUT/roads.gpkg
places|points|$C_PLACES|0|$F_PLACES|$OUT/places.gpkg"

# Buildings stay a separate pass: planet-wide they are ~179 GB on their own, more than every other
# theme combined, and they only render at z15+. Opt in per region:
#   BUILDING_REGIONS="europe north-america"
BUILDING_REGIONS="${BUILDING_REGIONS:-}"
if [ -n "$BUILDING_REGIONS" ]; then
    PASSES="$PASSES
buildings|multipolygons|building|0|building IS NOT NULL|$OUT/buildings.gpkg"
fi

# derived theme | filter applied to the staging file
DERIVED="countries|$F_COUNTRIES
water|$F_WATER
landuse|$F_LANDUSE"

ONLY_PASSES="${ONLY_PASSES:-}"
REGIONS="${REGIONS:-}"
SKIP_DERIVE="${SKIP_DERIVE:-}"

command -v ogr2ogr >/dev/null || die "ogr2ogr not on PATH (install gdal)"
[ -d "$SRC" ] || die "$SRC not found"
mkdir -p "$OUT" "$CPL_TMPDIR"
touch "$STATE"

mapfile -t PBFS < <(
    find "$SRC" -maxdepth 1 -name '*.osm.pbf' | sort | while read -r p; do
        if [ -z "$REGIONS" ]; then
            printf '%s\n' "$p"
        else
            for want in $REGIONS; do
                case "$(basename "$p")" in *"$want"*) printf '%s\n' "$p"; break ;; esac
            done
        fi
    done
)
[ "${#PBFS[@]}" -gt 0 ] || die "no .osm.pbf files in $SRC${REGIONS:+ matching '$REGIONS'}"

done_already() { grep -qxF "$1" "$STATE"; }
mark_done() { printf '%s\n' "$1" >> "$STATE"; }

feature_count() {
    [ -f "$1" ] || { echo 0; return; }
    ogrinfo -so "$1" "$2" 2>/dev/null | sed -n 's/^Feature Count: //p' | head -1 | grep -E '^[0-9]+$' || echo 0
}

has_spatial_index() {
    ogrinfo "$1" -sql "SELECT name FROM sqlite_master WHERE type='table' AND name='rtree_${2}_geom'" \
        2>/dev/null | grep -q "rtree_${2}_geom"
}

avail_gb=$(df -BG --output=avail "$OUT" | tail -1 | tr -dc '0-9')
log "source across ${#PBFS[@]} extracts, ${avail_gb}G free at $OUT (compress_nodes=$OSM_COMPRESS_NODES)"

# --- source passes ------------------------------------------------------------------------------
while IFS='|' read -r pass layer columns simplify filter dest; do
    [ -n "$pass" ] || continue
    if [ -n "$ONLY_PASSES" ]; then
        wanted=no
        for want in $ONLY_PASSES; do
            if [ "$pass" = "$want" ]; then wanted=yes; fi
        done
        [ "$wanted" = yes ] || continue
    fi

    log "=== pass: $pass -> $(basename "$dest")"
    for pbf in "${PBFS[@]}"; do
        region=$(basename "$pbf" .osm.pbf)
        key="$pass/$region"
        if done_already "$key"; then
            log "  skip $region (already done)"
            continue
        fi
        if [ "$pass" = "buildings" ]; then
            wanted=no
            for want in $BUILDING_REGIONS; do
                case "$region" in *"$want"*) wanted=yes ;; esac
            done
            if [ "$wanted" = no ]; then
                log "  skip $region (not in BUILDING_REGIONS)"
                continue
            fi
        fi

        if [ -f "$dest" ]; then
            mode=(-append)
        else
            mode=(-lco "GEOMETRY_NAME=geom")
        fi
        promote=()
        [ "$layer" = "multipolygons" ] && promote=(-nlt PROMOTE_TO_MULTI)
        simp=()
        [ "$simplify" != "0" ] && simp=(-simplify "$simplify")
        # Only the places pass needs a non-stock attribute list (population, name:en). Scoped to
        # that pass rather than exported globally so the already-completed polygon and line passes
        # keep producing byte-identical output if they are ever re-run.
        conf=()
        [ "$pass" = "places" ] && conf=(--config OSM_CONFIG_FILE "$HERE/osmconf-places.ini")

        # Column and row selection go through -sql, not -select/-where: ogr2ogr rejects -select once
        # -append is present, which only bites from the second region onward and would otherwise
        # leave each output holding one region out of eight.
        log "  $region ..."
        start=$(date +%s)
        before=$(feature_count "$dest" "$pass")

        # Unpiped, so the exit status is real: `ogr2ogr | grep || true` reports grep's status.
        set +e
        ogr2ogr -f GPKG "$dest" "$pbf" \
            "${conf[@]}" "${mode[@]}" "${promote[@]}" "${simp[@]}" \
            -sql "SELECT $columns FROM $layer WHERE $filter" \
            -nln "$pass" -gt 65536 \
            > "$CPL_TMPDIR/ogr2ogr-$pass.log" 2>&1
        rc=$?
        set -e
        grep -viE 'Non closed ring|organizePolygons|^[0-9.]*$|^$' "$CPL_TMPDIR/ogr2ogr-$pass.log" | tail -5 || true
        [ "$rc" -eq 0 ] || die "ogr2ogr failed on $pass/$region (exit $rc) — see log above"
        [ -f "$dest" ] || die "$pass/$region produced no output (check CPL_TMPDIR space)"

        after=$(feature_count "$dest" "$pass")
        mark_done "$key"
        log "  $region done in $(( $(date +%s) - start ))s, +$(( after - before )) features, $(du -h "$dest" | cut -f1) total"
    done
done <<< "$PASSES"

# --- split the staging file into themes ----------------------------------------------------------
# Pure GPKG -> GPKG with an attribute filter: no OSM driver, no node index, so this is minutes even
# for tens of GB. Each theme gets its own compact table and R-tree, which is what keeps per-tile
# queries fast — a single shared table would make every low-zoom water query scan the landuse rows
# too.
if [ -n "$SKIP_DERIVE" ]; then
    log "SKIP_DERIVE set; leaving $(basename "$STAGE") unsplit"
elif [ -n "$ONLY_PASSES" ] && ! grep -qw poly <<< "$ONLY_PASSES"; then
    log "poly pass not in ONLY_PASSES; skipping derive"
elif [ ! -f "$STAGE" ]; then
    log "no staging file yet; skipping derive"
else
    log "=== deriving themes from $(basename "$STAGE")"
    while IFS='|' read -r theme filter; do
        [ -n "$theme" ] || continue
        dest="$OUT/$theme.gpkg"
        log "  $theme ..."
        start=$(date +%s)
        rm -f "$dest"
        set +e
        ogr2ogr -f GPKG "$dest" "$STAGE" \
            -lco "GEOMETRY_NAME=geom" -nln "$theme" -gt 65536 \
            -sql "SELECT * FROM poly WHERE $filter" \
            > "$CPL_TMPDIR/derive-$theme.log" 2>&1
        rc=$?
        set -e
        tail -3 "$CPL_TMPDIR/derive-$theme.log" || true
        [ "$rc" -eq 0 ] || die "derive failed for $theme (exit $rc)"
        ogrinfo "$dest" -sql "ANALYZE" >/dev/null 2>&1 || true

        n=$(feature_count "$dest" "$theme")
        [ "$n" -gt 0 ] || die "$theme derived 0 features — check its filter against $C_POLY"
        has_spatial_index "$dest" "$theme" || die "$theme has no spatial index; tiles would table-scan"
        log "  $theme: $n features, $(du -h "$dest" | cut -f1), index ok, $(( $(date +%s) - start ))s"
    done <<< "$DERIVED"
fi

# roads and places are produced directly by their passes, so verify them here rather than in the
# derive loop, which only covers the themes split out of the staging file.
for direct in roads places; do
    [ -f "$OUT/$direct.gpkg" ] || continue
    has_spatial_index "$OUT/$direct.gpkg" "$direct" || die "$direct has no spatial index"
done

log "done"
ls -lh "$OUT"/*.gpkg 2>/dev/null | awk '{printf "  %-22s %s\n", $9, $5}'
cat <<EOF

Staging file $(basename "$STAGE") is kept so the themes can be re-split with different filters
cheaply (rerun with SKIP_DERIVE= to redo just the split). Delete it to reclaim the space.

Next:
  scripts/load-natural-earth-ocean.sh         # if public.ocean/marine_areas do not exist yet
  scripts/load-natural-earth-states.sh        # if public.states does not exist yet
  python3 scripts/patch-project.py --base --detail
  edit .env: TILE_LAYER_ROUTES=countries@0-3=simple-countries
  sh scripts/sync-project-version.sh && docker compose up -d --build renderer && docker compose up -d api
EOF
