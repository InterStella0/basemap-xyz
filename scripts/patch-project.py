#!/usr/bin/env python3
"""Patch qgis-server/project/project.qgz for the zoom-switched dark basemap.

project.qgz is a zip of project.qgs (XML) plus a styles DB, so it is a binary blob in git with no
reviewable diff. This script is the reviewable source of truth for the cartography instead: it is
idempotent, so re-running it after a QGIS Desktop edit re-applies the same intent.

Two phases, because the detail layers cannot exist before their data does — QGIS Server runs with
QGIS_SERVER_IGNORE_BAD_LAYERS=0, so a layer pointing at a missing GeoPackage takes down the whole
project, not just that layer.

  --base     (always safe) rename `countries` -> `simple-countries`, and extend the WMTS pyramid
             from z0-16 to z0-20 so MAX_ZOOM=20 in .env is actually served.
  --detail   add the OSM detail layers as a group published under the single WMTS name `countries`.
             Requires the GeoPackages from scripts/build-osm-detail.sh to exist.

Run scripts/sync-project-version.sh afterwards to flush the tile cache.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECT = REPO / "qgis-server" / "project" / "project.qgz"

# Scale denominator of XYZ zoom 0 in EPSG:3857, and the pyramid depth we publish. QGIS derives each
# level as z0 / 2**z, so this is the same ladder the API's MIN_ZOOM/MAX_ZOOM talk about.
Z0_SCALE = 559082264.0287179
LEVELS = 21  # z0..z20 inclusive


def scale_at(z: float) -> float:
    return Z0_SCALE / (2**z)


def visibility_bound(min_z: int) -> int:
    """The scale denominator to hang a "draws from zoom `min_z`" rule on.

    Deliberately half a zoom level coarser than `scale_at(min_z)`. QGIS Server computes the request
    scale itself from the tile matrix and its own DPI assumption, so it lands *near* our number
    rather than exactly on it; a bound placed on the level boundary flips on rounding and the layer
    silently starts one zoom late. Half a level of slack is far smaller than the 2x gap between
    levels, so it cannot reach z-1, and it removes the coin toss.
    """
    return int(scale_at(min_z - 0.5))


def _frac(v: float) -> str:
    """QGIS colour fractions: 7dp, trailing zeros stripped ('1', not '1.0000000')."""
    s = f"{v:.7f}".rstrip("0").rstrip(".")
    return s or "0"


def colour(hex_rgb: str, alpha: int = 255) -> str:
    """'#0f0f0f' -> '15,15,15,255,rgb:0.0588235,0.0588235,0.0588235,1' (QGIS's own encoding)."""
    h = hex_rgb.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return (
        f"{r},{g},{b},{alpha},rgb:"
        f"{_frac(r / 255)},{_frac(g / 255)},{_frac(b / 255)},{_frac(alpha / 255)}"
    )


def uid() -> str:
    return "{" + str(uuid.uuid4()) + "}"


def opt_map(**values: str) -> ET.Element:
    """<Option type="Map"> with a QString entry per keyword, which is how QGIS stores symbol layers."""
    m = ET.Element("Option", {"type": "Map"})
    for k, v in values.items():
        ET.SubElement(m, "Option", {"name": k, "type": "QString", "value": v})
    return m


def ddp(tag: str = "data_defined_properties") -> ET.Element:
    """The empty data-defined-properties block every symbol and symbol layer carries."""
    e = ET.Element(tag)
    m = ET.SubElement(e, "Option", {"type": "Map"})
    ET.SubElement(m, "Option", {"name": "name", "type": "QString", "value": ""})
    ET.SubElement(m, "Option", {"name": "properties"})
    ET.SubElement(m, "Option", {"name": "type", "type": "QString", "value": "collection"})
    return e


def symbol(kind: str, name: str, layers: list[ET.Element]) -> ET.Element:
    s = ET.Element(
        "symbol",
        {
            "alpha": "1",
            "clip_to_extent": "1",
            "force_rhr": "0",
            "frame_rate": "10",
            "is_animated": "0",
            "name": name,
            "type": kind,
        },
    )
    s.append(ddp())
    for layer in layers:
        s.append(layer)
    return s


def fill_layer(fill: str, stroke: str | None, stroke_width: float) -> ET.Element:
    e = ET.Element(
        "layer",
        {"class": "SimpleFill", "enabled": "1", "id": uid(), "locked": "0", "pass": "0"},
    )
    e.append(
        opt_map(
            border_width_map_unit_scale="3x:0,0,0,0,0,0",
            color=colour(fill),
            joinstyle="bevel",
            offset="0,0",
            offset_map_unit_scale="3x:0,0,0,0,0,0",
            offset_unit="MM",
            outline_color=colour(stroke) if stroke else colour("#000000", 0),
            outline_style="solid" if stroke else "no",
            outline_width=str(stroke_width),
            outline_width_unit="MM",
            style="solid",
        )
    )
    e.append(ddp())
    return e


def line_layer(stroke: str, width: float) -> ET.Element:
    e = ET.Element(
        "layer",
        {"class": "SimpleLine", "enabled": "1", "id": uid(), "locked": "0", "pass": "0"},
    )
    e.append(
        opt_map(
            align_dash_pattern="0",
            capstyle="round",
            customdash="5;2",
            customdash_map_unit_scale="3x:0,0,0,0,0,0",
            customdash_unit="MM",
            dash_pattern_offset="0",
            dash_pattern_offset_map_unit_scale="3x:0,0,0,0,0,0",
            dash_pattern_offset_unit="MM",
            draw_inside_polygon="0",
            joinstyle="round",
            line_color=colour(stroke),
            line_style="solid",
            line_width=str(width),
            line_width_unit="MM",
            offset="0",
            offset_map_unit_scale="3x:0,0,0,0,0,0",
            offset_unit="MM",
            ring_filter="0",
            trim_distance_end="0",
            trim_distance_end_map_unit_scale="3x:0,0,0,0,0,0",
            trim_distance_end_unit="MM",
            trim_distance_start="0",
            trim_distance_start_map_unit_scale="3x:0,0,0,0,0,0",
            trim_distance_start_unit="MM",
            tweak_dash_pattern_on_corners="0",
            use_custom_dash="0",
            width_map_unit_scale="3x:0,0,0,0,0,0",
        )
    )
    e.append(ddp())
    return e


def single_renderer(sym: ET.Element) -> ET.Element:
    r = ET.Element(
        "renderer-v2",
        {
            "enableorderby": "0",
            "forceraster": "0",
            "referencescale": "-1",
            "symbollevels": "0",
            "type": "singleSymbol",
        },
    )
    syms = ET.SubElement(r, "symbols")
    syms.append(sym)
    return r


def rule_renderer(rules: list[dict]) -> ET.Element:
    """Rule-based renderer with one fixed symbol per rule.

    Used for roads so each class gets its own width without data-defined expressions — a plain
    SimpleLine per rule is far easier to keep valid across QGIS versions than a width expression.
    """
    r = ET.Element(
        "renderer-v2",
        {
            "enableorderby": "0",
            "forceraster": "0",
            "referencescale": "-1",
            "symbollevels": "0",
            "type": "RuleRenderer",
        },
    )
    container = ET.SubElement(r, "rules", {"key": uid()})
    syms = ET.SubElement(r, "symbols")
    for i, rule in enumerate(rules):
        attrs = {
            "filter": rule["filter"],
            "key": uid(),
            "label": rule["label"],
            "symbol": str(i),
        }
        if rule.get("min_z") is not None:
            # scalemindenom is the zoomed-IN bound; a rule shows only when the map is at least this
            # zoomed in, which is how we keep residential streets out of z12 tiles.
            attrs["scalemaxdenom"] = str(visibility_bound(rule["min_z"]))
        ET.SubElement(container, "rule", attrs)
        syms.append(symbol("line", str(i), [line_layer(rule["colour"], rule["width"])]))
    return r


# --- the detail themes ----------------------------------------------------------------------------
# Colours extend the established theme: land #0f0f0f on a black canvas with a white border. Water
# reads as a hole punched in the land, landuse as a barely-there lift, buildings one step above land,
# roads the only bright thing. min_z is the XYZ zoom the layer starts drawing at; below it QGIS does
# not even query the GeoPackage, which is the entire point of the exercise.
THEMES = [
    {
        "name": "roads",
        "gpkg": "roads",
        "geometry": "Line",
        "wkb": "LineString",
        "min_z": 7,
        "renderer": lambda: rule_renderer(
            [
                {
                    "label": "major",
                    "filter": "\"highway\" IN ('motorway','trunk','primary','motorway_link','trunk_link','primary_link')",
                    "colour": "#4a4a4a",
                    "width": 0.5,
                    "min_z": 7,
                },
                {
                    "label": "secondary",
                    "filter": "\"highway\" IN ('secondary','tertiary','secondary_link','tertiary_link')",
                    "colour": "#3d3d3d",
                    "width": 0.34,
                    "min_z": 10,
                },
                {
                    "label": "minor",
                    "filter": "ELSE",
                    "colour": "#2e2e2e",
                    "width": 0.22,
                    "min_z": 13,
                },
            ]
        ),
    },
    {
        "name": "buildings",
        "gpkg": "buildings",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "min_z": 15,
        "renderer": lambda: single_renderer(
            symbol("fill", "0", [fill_layer("#1a1a1a", "#242424", 0.06)])
        ),
    },
    {
        "name": "water",
        "gpkg": "water",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "min_z": 5,
        "renderer": lambda: single_renderer(
            symbol("fill", "0", [fill_layer("#07090c", None, 0)])
        ),
    },
    {
        "name": "landuse",
        "gpkg": "landuse",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "min_z": 8,
        "renderer": lambda: single_renderer(
            symbol("fill", "0", [fill_layer("#131313", None, 0)])
        ),
    },
    {
        "name": "land-base",
        "gpkg": "countries",  # unused; see theme_datasource()
        "datasource": (
            "service='mellabasemap' key='fid' srid=4326 type=MultiPolygon "
            "checkPrimaryKeyUnicity='1' table=\"public\".\"countries\" (geom)"
        ),
        "provider": "postgres",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "min_z": None,  # the land base of the detail group; always drawn
        "renderer": lambda: single_renderer(
            symbol("fill", "0", [fill_layer("#0f0f0f", "#ffffff", 0.26)])
        ),
    },
]

WGS84_WKT = (
    'GEOGCRS["WGS 84",ENSEMBLE["World Geodetic System 1984 ensemble",'
    'MEMBER["World Geodetic System 1984 (Transit)"],MEMBER["World Geodetic System 1984 (G730)"],'
    'MEMBER["World Geodetic System 1984 (G873)"],MEMBER["World Geodetic System 1984 (G1150)"],'
    'MEMBER["World Geodetic System 1984 (G1674)"],MEMBER["World Geodetic System 1984 (G1762)"],'
    'MEMBER["World Geodetic System 1984 (G2139)"],MEMBER["World Geodetic System 1984 (G2296)"],'
    'ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]],ENSEMBLEACCURACY[2.0]],'
    'PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]],CS[ellipsoidal,2],'
    'AXIS["geodetic latitude (Lat)",north,ORDER[1],ANGLEUNIT["degree",0.0174532925199433]],'
    'AXIS["geodetic longitude (Lon)",east,ORDER[2],ANGLEUNIT["degree",0.0174532925199433]],'
    'USAGE[SCOPE["Horizontal component of 3D system."],AREA["World."],BBOX[-90,-180,90,180]],'
    'ID["EPSG",4326]]'
)


def srs_element(tag: str = "srs") -> ET.Element:
    e = ET.Element(tag)
    s = ET.SubElement(e, "spatialrefsys", {"nativeFormat": "Wkt"})
    for name, text in [
        ("wkt", WGS84_WKT),
        ("proj4", "+proj=longlat +datum=WGS84 +no_defs"),
        ("srsid", "3452"),
        ("srid", "4326"),
        ("authid", "EPSG:4326"),
        ("description", "WGS 84"),
        ("projectionacronym", "longlat"),
        ("ellipsoidacronym", "EPSG:7030"),
        ("geographicflag", "true"),
    ]:
        ET.SubElement(s, name).text = text
    return e


def extent_element(tag: str) -> ET.Element:
    """World extent, written explicitly because QGIS_SERVER_TRUST_LAYER_METADATA=1 makes QGIS trust
    this instead of opening every GeoPackage at project load — the main cold-start win."""
    e = ET.Element(tag)
    for name, value in [
        ("xmin", "-180"),
        ("ymin", "-90"),
        ("xmax", "180"),
        ("ymax", "90"),
    ]:
        ET.SubElement(e, name).text = value
    return e


def theme_datasource(theme: dict) -> str:
    """Where a themed layer reads from.

    Most themes are GeoPackages under the /data bind mount. `land-base` is the exception: it reuses
    the PostGIS Natural Earth table, because OSM cannot supply a reliable land polygon here. GDAL's
    OSM driver fails to assemble the largest admin_level=2 boundary relations — measured on the full
    planet build, France, Spain, the USA and Russia all came out with no polygon at all (4 of 12
    sample cities had no land), emitting "Non closed ring detected" as it gave up. Those are exactly
    the countries with far-flung overseas territories or an antimeridian crossing. Natural Earth is
    generalized, so coastlines are coarser than OSM would be at z14+, but it is complete, and
    complete beats precise for the layer everything else is drawn on top of.
    """
    if "datasource" in theme:
        return theme["datasource"]
    return f"/data/{theme['gpkg']}.gpkg|layername={theme['gpkg']}"


def make_maplayer(theme: dict, layer_id: str) -> ET.Element:
    scale_based = theme["min_z"] is not None
    attrs = {
        "autoRefreshMode": "Disabled",
        "autoRefreshTime": "0",
        "geometry": theme["geometry"],
        "hasScaleBasedVisibilityFlag": "1" if scale_based else "0",
        "labelsEnabled": "0",
        "layerType": "Vector",
        "legendPlaceholderImage": "",
        # QGIS names these backwards from intuition: minScale is the zoomed-OUT bound (largest
        # denominator at which the layer still draws), maxScale the zoomed-in bound. 0 = unbounded.
        "maxScale": "0",
        "minScale": str(visibility_bound(theme["min_z"])) if scale_based else "0",
        "readOnly": "0",
        "refreshOnNotifyEnabled": "0",
        "refreshOnNotifyMessage": "",
        # Simplification is what keeps coastline and landuse polygons affordable at low zoom.
        "simplifyAlgorithm": "0",
        "simplifyDrawingHints": "1",
        "simplifyDrawingTol": "1",
        "simplifyLocal": "1",
        "simplifyMaxScale": "1",
        "styleCategories": "AllStyleCategories",
        "symbologyReferenceScale": "-1",
        "type": "vector",
        "wkbType": theme["wkb"],
    }
    ml = ET.Element("maplayer", attrs)
    ml.append(extent_element("extent"))
    ml.append(extent_element("wgs84extent"))
    ET.SubElement(ml, "id").text = layer_id
    ET.SubElement(ml, "datasource").text = theme_datasource(theme)
    ET.SubElement(ml, "layername").text = theme["name"]
    ml.append(srs_element())
    ET.SubElement(ml, "provider", {"encoding": "UTF-8"}).text = theme.get("provider", "ogr")
    ET.SubElement(ml, "vectorjoins")
    ET.SubElement(ml, "layerDependencies")
    ET.SubElement(ml, "dataDependencies")
    ET.SubElement(ml, "expressionfields")
    mgr = ET.SubElement(ml, "map-layer-style-manager", {"current": "default"})
    ET.SubElement(mgr, "map-layer-style", {"name": "default"})
    ET.SubElement(ml, "auxiliaryLayer")
    flags = ET.SubElement(ml, "flags")
    for f, v in [("Identifiable", "0"), ("Removable", "1"), ("Searchable", "0"), ("Private", "0")]:
        ET.SubElement(flags, f).text = v
    ml.append(theme["renderer"]())
    ET.SubElement(ml, "customproperties").append(ET.Element("Option"))
    ET.SubElement(ml, "blendMode").text = "0"
    ET.SubElement(ml, "featureBlendMode").text = "0"
    ET.SubElement(ml, "layerOpacity").text = "1"
    return ml


# --- patch operations -----------------------------------------------------------------------------


def prop(root: ET.Element, path: str) -> ET.Element | None:
    """Look up a project property by its slash path, e.g. 'WMTSGrids/Config'.

    QGIS keys properties by a `name` attribute rather than the tag (every element is literally
    <properties>), and nests them: WMTSGrids/Config is a <properties name="Config"> inside a
    <properties name="WMTSGrids">. A flat scan for the joined string finds nothing.
    """
    node = root.find("properties")
    if node is None:
        return None
    for segment in path.split("/"):
        node = next(
            (c for c in node.findall("properties") if c.get("name") == segment),
            None,
        )
        if node is None:
            return None
    return node


def set_prop_values(root: ET.Element, name: str, values: list[str]) -> None:
    el = prop(root, name)
    if el is None:
        raise SystemExit(f"patch-project: property {name!r} not found in project.qgs")
    for v in list(el.findall("value")):
        el.remove(v)
    for v in values:
        ET.SubElement(el, "value").text = v


def rename_layer(root: ET.Element, old: str, new: str) -> bool:
    """Rename by display name in all three places QGIS keeps it. The layer *id* is left alone, so
    WMTSLayers/Layer (which holds ids) keeps pointing at the right layer."""
    changed = False
    for el in root.iter("layer-tree-layer"):
        if el.get("name") == old:
            el.set("name", new)
            changed = True
    for el in root.iter("legendlayer"):
        if el.get("name") == old:
            el.set("name", new)
            changed = True
    for ml in root.iter("maplayer"):
        ln = ml.find("layername")
        if ln is not None and ln.text == old:
            ln.text = new
            changed = True
    return changed


def patch_base(root: ET.Element) -> None:
    if rename_layer(root, "countries", "simple-countries"):
        print("  renamed layer 'countries' -> 'simple-countries'")
    else:
        print("  layer already named 'simple-countries'")

    # The pyramid. Config is '<CRS>,<xmin>,<ymin>,<z0 scale>,<levels>'; the trailing level count is
    # what truncated the matrix set at z17, and WMTSMinScale=5004 cut it again to z16.
    cfg = prop(root, "WMTSGrids/Config")
    if cfg is None:
        raise SystemExit("patch-project: WMTSGrids/Config not found; publish WMTS in QGIS first")
    new_values = []
    for v in cfg.findall("value"):
        parts = (v.text or "").split(",")
        if len(parts) == 5:
            parts[4] = str(LEVELS)
        new_values.append(",".join(parts))
    set_prop_values(root, "WMTSGrids/Config", new_values)
    print(f"  WMTS pyramid depth -> {LEVELS} levels (z0..z{LEVELS - 1})")

    min_scale = prop(root, "WMTSMinScale")
    if min_scale is not None and min_scale.text != "0":
        print(f"  WMTSMinScale {min_scale.text} -> 0 (was truncating the pyramid at z16)")
        min_scale.text = "0"


def patch_detail(root: ET.Element, data_dir: Path, require_data: bool) -> None:
    # Only reference GeoPackages that actually exist. QGIS Server runs with
    # QGIS_SERVER_IGNORE_BAD_LAYERS=0, so a layer pointing at a missing file fails the entire
    # project rather than just that layer — and `buildings` is legitimately optional
    # (BUILDING_REGIONS in build-osm-detail.sh), so its absence is normal, not an error.
    present = [t for t in THEMES if (data_dir / f"{t['gpkg']}.gpkg").exists()]
    absent = [t["gpkg"] for t in THEMES if t not in present]

    if not present:
        raise SystemExit(
            f"patch-project: no GeoPackages found in {data_dir}. "
            "Run scripts/build-osm-detail.sh first."
        )
    if absent and require_data:
        print(f"  skipping themes with no data: {', '.join(sorted(absent))}")
    elif absent:
        print(f"  WARNING: referencing absent GeoPackages: {', '.join(sorted(absent))}")
        present = THEMES
    themes = present

    tree = root.find("layer-tree-group")
    legend = root.find("legend")
    layers = root.find("projectlayers")
    order = root.find("layerorder")
    if tree is None or legend is None or layers is None:
        raise SystemExit("patch-project: project.qgs is missing layer-tree-group/legend/projectlayers")

    # Idempotence: drop any previous run's group and layers before re-adding.
    for grp in list(tree.findall("layer-tree-group")):
        if grp.get("name") == "countries":
            tree.remove(grp)
    for grp in list(legend.findall("legendgroup")):
        if grp.get("name") == "countries":
            legend.remove(grp)
    known = {t["name"] for t in THEMES}
    stale_ids = set()
    for ml in list(layers.findall("maplayer")):
        ln = ml.find("layername")
        if ln is not None and ln.text in known:
            id_el = ml.find("id")
            if id_el is not None and id_el.text:
                stale_ids.add(id_el.text)
            layers.remove(ml)
    if order is not None:
        for item in list(order.findall("layer")):
            if item.get("id") in stale_ids:
                order.remove(item)

    group = ET.Element(
        "layer-tree-group",
        {"checked": "Qt::Checked", "expanded": "1", "groupLayer": "", "name": "countries"},
    )
    legend_group = ET.Element(
        "legendgroup", {"checked": "Qt::Checked", "name": "countries", "open": "true"}
    )

    for theme in themes:
        layer_id = "{}_{}".format(theme["name"].replace("-", "_"), uuid.uuid4().hex)
        datasource = f"/data/{theme['gpkg']}.gpkg|layername={theme['gpkg']}"
        ET.SubElement(
            group,
            "layer-tree-layer",
            {
                "checked": "Qt::Checked",
                "expanded": "0",
                "id": layer_id,
                "legend_exp": "",
                "legend_split_behavior": "0",
                "name": theme["name"],
                "patch_size": "-1,-1",
                "providerKey": "ogr",
                "source": datasource,
            },
        ).append(ET.Element("customproperties"))
        lg = ET.SubElement(
            legend_group,
            "legendlayer",
            {
                "checked": "Qt::Checked",
                "drawingOrder": "-1",
                "name": theme["name"],
                "open": "false",
                "showFeatureCount": "0",
            },
        )
        ET.SubElement(ET.SubElement(lg, "filegroup", {"hidden": "false", "open": "false"}),
                      "legendlayerfile",
                      {"isInOverview": "0", "layerid": layer_id, "visible": "1"})
        layers.append(make_maplayer(theme, layer_id))
        if order is not None:
            order.append(ET.Element("layer", {"id": layer_id}))

    # Insert above the existing simple-countries entry so the detail group draws on top of nothing —
    # the two are never visible at the same zoom anyway, but tree order decides group nesting.
    tree.insert(0, group)
    legend.insert(0, legend_group)
    print(f"  added group 'countries' with {len(themes)} layers: " + ", ".join(t["name"] for t in themes))

    # Publish the group as one WMTS layer. WMTSLayers/Layer holds layer *ids*; the Group key holds
    # group *names*. Publishing the group is what lets five styled layers answer to one name.
    set_prop_values(root, "WMTSLayers/Group", ["countries"])
    set_prop_values(root, "WMTSPngLayers/Group", ["countries"])
    print("  published group 'countries' for WMTS (png)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", action="store_true", help="rename the layer and fix the WMTS pyramid depth")
    ap.add_argument("--detail", action="store_true", help="add the OSM detail layer group")
    ap.add_argument("--data-dir", default="/mnt/meow/OSM/out", help="where the GeoPackages live on the host")
    ap.add_argument("--allow-missing-data", action="store_true", help="patch --detail even if the GeoPackages are absent")
    ap.add_argument("--project", default=str(PROJECT))
    args = ap.parse_args()

    if not (args.base or args.detail):
        ap.error("nothing to do: pass --base and/or --detail")

    project = Path(args.project)
    if not project.exists():
        raise SystemExit(f"patch-project: {project} not found")

    with zipfile.ZipFile(project) as zf:
        names = zf.namelist()
        qgs_name = next((n for n in names if n.endswith(".qgs")), None)
        if qgs_name is None:
            raise SystemExit("patch-project: no .qgs inside the .qgz")
        blobs = {n: zf.read(n) for n in names}

    root = ET.fromstring(blobs[qgs_name].decode("utf-8"))

    if args.base:
        print("--base:")
        patch_base(root)
    if args.detail:
        print("--detail:")
        patch_detail(root, Path(args.data_dir), not args.allow_missing_data)

    backup = project.with_suffix(".qgz.bak")
    shutil.copy2(project, backup)

    blobs[qgs_name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    tmp = project.with_suffix(".qgz.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in blobs.items():
            zf.writestr(name, data)
    tmp.replace(project)

    print(f"\nwrote {project} (previous version kept at {backup.name})")
    # --build is required: the renderer image COPYs project.qgz rather than mounting it.
    print("next: sh scripts/sync-project-version.sh && docker compose up -d --build renderer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
