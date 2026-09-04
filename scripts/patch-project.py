#!/usr/bin/env python3
"""Patch qgis-server/project/project.qgz for the zoom-switched dark basemap.

project.qgz is a zip of project.qgs (XML) plus a styles DB, so it is a binary blob in git with no
reviewable diff. This script is the reviewable source of truth for the cartography instead: it is
idempotent, so re-running it after a QGIS Desktop edit re-applies the same intent.

Two phases, because the detail layers cannot exist before their data does — QGIS Server runs with
QGIS_SERVER_IGNORE_BAD_LAYERS=0, so a layer pointing at a missing source takes down the whole
project, not just that layer.

  --base     replace the old standalone country layer with a `simple-countries` group containing
             Natural Earth land, ocean and low-zoom labels, and extend the WMTS pyramid from z0-16
             to z0-20 so MAX_ZOOM=20 in .env is actually served.
  --detail   add the OSM detail layers, Natural Earth land/ocean and every label layer as a group
             published under the single WMTS name `countries`. Requires the GeoPackages from
             scripts/build-osm-detail.sh and the PostGIS tables loaded by the two
             scripts/load-natural-earth-*.sh helpers.

Run scripts/sync-project-version.sh afterwards to flush the tile cache.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
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


def fill_layer(
    fill: str,
    stroke: str | None,
    stroke_width: float,
    *,
    style: str = "solid",
    alpha: int = 255,
    outline_style: str = "solid",
) -> ET.Element:
    """A SimpleFill. `style="no"` gives an outline-only polygon (the states boundary), and
    `style="no"` with no stroke gives a polygon that draws nothing at all — which is what a
    label-only layer needs, since a maplayer must still carry a renderer."""
    e = ET.Element(
        "layer",
        {"class": "SimpleFill", "enabled": "1", "id": uid(), "locked": "0", "pass": "0"},
    )
    e.append(
        opt_map(
            border_width_map_unit_scale="3x:0,0,0,0,0,0",
            color=colour(fill, alpha),
            joinstyle="bevel",
            offset="0,0",
            offset_map_unit_scale="3x:0,0,0,0,0,0",
            offset_unit="MM",
            outline_color=colour(stroke) if stroke else colour("#000000", 0),
            outline_style=outline_style if stroke else "no",
            outline_width=str(stroke_width),
            outline_width_unit="MM",
            style=style,
        )
    )
    e.append(ddp())
    return e


def marker_layer(fill: str, size: float, *, alpha: int = 255) -> ET.Element:
    """A plain filled circle. Places get a small dot so the name has something to belong to; a
    label floating in empty space reads as a typo rather than a town."""
    e = ET.Element(
        "layer",
        {"class": "SimpleMarker", "enabled": "1", "id": uid(), "locked": "0", "pass": "0"},
    )
    e.append(
        opt_map(
            angle="0",
            cap_style="square",
            color=colour(fill, alpha),
            horizontal_anchor_point="1",
            joinstyle="bevel",
            name="circle",
            offset="0,0",
            offset_map_unit_scale="3x:0,0,0,0,0,0",
            offset_unit="MM",
            outline_color=colour("#000000", 0),
            outline_style="no",
            outline_width="0",
            outline_width_map_unit_scale="3x:0,0,0,0,0,0",
            outline_width_unit="MM",
            scale_method="diameter",
            size=str(size),
            size_map_unit_scale="3x:0,0,0,0,0,0",
            size_unit="MM",
            vertical_anchor_point="1",
        )
    )
    e.append(ddp())
    return e


def invisible_renderer(kind: str) -> ET.Element:
    """A renderer that draws nothing. Label-only layers still need one — QGIS will not load a
    vector maplayer without a <renderer-v2> — so they get a symbol with every stroke and fill
    switched off."""
    if kind == "marker":
        layer = marker_layer("#000000", 0, alpha=0)
    else:
        layer = fill_layer("#000000", None, 0, style="no", alpha=0)
    return single_renderer(symbol(kind, "0", [layer]))


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


# --- labelling -------------------------------------------------------------------------------------
# QGIS keeps label settings in a <labeling> element on the maplayer, parallel to <renderer-v2>. The
# attribute names below are copied verbatim from a project QGIS 4.2 wrote itself
# (/usr/share/qgis/resources/data/qgis-hackfests.qml); QGIS silently falls back to defaults for a
# <settings> block it cannot parse, so an invented attribute name shows up as "the labels are wrong"
# rather than as an error.


def plain_colour(hex_rgb: str, alpha: int = 255) -> str:
    """'#cfcfcf' -> '207,207,207,255'. Text colours use this short form, not `colour()`'s."""
    h = hex_rgb.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"{r},{g},{b},{alpha}"


def labeling(spec: dict, geometry: str) -> ET.Element:
    """<labeling type="simple"> from a theme's `label` dict.

    Only simple labelling is emitted. Zoom staging is done by splitting a source across several map
    layers with a datasource subset and per-layer `minScale`, which is the mechanism the renderers
    here already rely on — rather than by hand-writing rule-based-labelling XML, which is a second
    schema to get right for no extra capability.
    """
    lab = ET.Element("labeling", {"type": "simple"})
    settings = ET.SubElement(lab, "settings")

    style = ET.SubElement(
        settings,
        "text-style",
        {
            "allowHtml": "0",
            "blendMode": "0",
            "capitalization": str(spec.get("capitalization", 0)),
            "fieldName": spec["field"],
            "fontFamily": spec.get("family", "DejaVu Sans"),
            "fontItalic": "1" if spec.get("italic") else "0",
            "fontKerning": "1",
            "fontLetterSpacing": str(spec.get("letter_spacing", 0)),
            "fontSize": str(spec["size"]),
            "fontSizeMapUnitScale": "3x:0,0,0,0,0,0",
            "fontSizeUnit": "Point",
            "fontStrikeout": "0",
            "fontUnderline": "0",
            "fontWeight": "75" if spec.get("bold") else "50",
            "fontWordSpacing": "0",
            "forcedBold": "0",
            "forcedItalic": "0",
            "isExpression": "1",
            "legendString": "Aa",
            "multilineHeight": "1",
            "multilineHeightUnit": "Percentage",
            "namedStyle": "Bold" if spec.get("bold") else "Regular",
            "previewBkgrdColor": "0,0,0,255",
            "textColor": plain_colour(spec["colour"]),
            "textOpacity": "1",
            "textOrientation": "horizontal",
            "useSubstitutions": "0",
        },
    )
    ET.SubElement(style, "families")
    # The halo. Everything here is drawn on near-black, but text also crosses roads, water and
    # landuse fills; without a buffer the thin strokes read as noise through the glyphs. Semi-opaque
    # rather than solid so it darkens what is under it instead of punching a hole in the map.
    ET.SubElement(
        style,
        "text-buffer",
        {
            "bufferBlendMode": "0",
            "bufferColor": plain_colour(spec.get("halo", "#000000"), spec.get("halo_alpha", 190)),
            "bufferDraw": "1",
            "bufferJoinStyle": "128",
            "bufferNoFill": "1",
            "bufferOpacity": "1",
            "bufferSize": str(spec.get("halo_size", 0.8)),
            "bufferSizeMapUnitScale": "3x:0,0,0,0,0,0",
            "bufferSizeUnits": "MM",
        },
    )
    ET.SubElement(
        style,
        "text-mask",
        {
            "maskEnabled": "0",
            "maskJoinStyle": "128",
            "maskOpacity": "1",
            "maskSize": "0",
            "maskSizeMapUnitScale": "3x:0,0,0,0,0,0",
            "maskSizeUnits": "MM",
            "maskType": "0",
            "maskedSymbolLayers": "",
        },
    )
    ET.SubElement(style, "background", {"shapeDraw": "0", "shapeType": "0"})
    ET.SubElement(style, "shadow", {"shadowDraw": "0"})
    style.append(ddp())
    ET.SubElement(style, "substitutions")

    ET.SubElement(
        settings,
        "text-format",
        {
            "addDirectionSymbol": "0",
            "autoWrapLength": "0",
            "decimals": "3",
            "formatNumbers": "0",
            "leftDirectionSymbol": "<",
            "multilineAlign": "3",
            "placeDirectionSymbol": "0",
            "plussign": "0",
            "reverseDirectionSymbol": "0",
            "rightDirectionSymbol": ">",
            "useMaxLineLengthForAutoWrap": "1",
            "wrapChar": "",
        },
    )

    layer_type = {"Polygon": "PolygonGeometry", "Line": "LineGeometry"}.get(geometry, "PointGeometry")
    ET.SubElement(
        settings,
        "placement",
        {
            "allowDegraded": "0",
            "centroidInside": "1",
            # THE load-bearing attribute for a tiled basemap. With centroidWhole="0" QGIS anchors a
            # polygon's label at the centroid of the part of it visible in the request extent — and
            # every 256x256 GetTile is its own extent, so a country gets relabelled in every single
            # tile it touches. "1" anchors on the whole geometry, so the label is drawn once, in the
            # tile that actually contains the centroid.
            "centroidWhole": "1",
            "dist": "0",
            "distMapUnitScale": "3x:0,0,0,0,0,0",
            "distUnits": "MM",
            "fitInPolygonOnly": "0",
            "geometryGenerator": "",
            "geometryGeneratorEnabled": "0",
            "geometryGeneratorType": "PointGeometry",
            "labelOffsetMapUnitScale": "3x:0,0,0,0,0,0",
            "lineAnchorClipping": "0",
            "lineAnchorPercent": "0.5",
            "lineAnchorTextPoint": "CenterOfText",
            "lineAnchorType": "0",
            "maxCurvedCharAngleIn": "25",
            "maxCurvedCharAngleOut": "-25",
            "offsetType": "0",
            "offsetUnits": "MM",
            "overlapHandling": "PreventOverlap",
            "overrunDistance": "0",
            "overrunDistanceMapUnitScale": "3x:0,0,0,0,0,0",
            "overrunDistanceUnit": "MM",
            # OverPoint: centred on the anchor. Points have no dot to dodge (places-city draws a
            # small one, and the halo keeps the text legible over it), polygons want the name in
            # the middle of the shape.
            "placement": "1",
            "placementFlags": "10",
            "layerType": layer_type,
            "polygonPlacementFlags": "2",
            "predefinedPositionOrder": "TR,TL,BR,BL,R,L,TSR,BSR",
            "preserveRotation": "1",
            # Higher wins a collision. Country names should survive a clash with a village.
            "priority": str(spec.get("priority", 5)),
            "quadOffset": "4",
            "repeatDistance": "0",
            "repeatDistanceMapUnitScale": "3x:0,0,0,0,0,0",
            "repeatDistanceUnits": "MM",
            "rotationAngle": "0",
            "rotationUnit": "AngleDegrees",
            "xOffset": "0",
            "yOffset": "0",
        },
    )

    ET.SubElement(
        settings,
        "rendering",
        {
            "drawLabels": "1",
            "fontLimitPixelSize": "0",
            "fontMaxPixelSize": "10000",
            "fontMinPixelSize": "3",
            # One label per multipolygon, not one per island. Without this every Indonesian island
            # and every Alaskan islet gets its own "Indonesia".
            "labelPerPart": "0",
            "limitNumLabels": "0",
            "maxNumLabels": "2000",
            "mergeLines": "0",
            # Suppresses the label when the feature itself is smaller than this on screen, in mm.
            # This is the zoom staging for polygon labels and it is free: Luxembourg drops out at
            # z4 and comes back at z6 without a second layer or a scale rule.
            "minFeatureSize": str(spec.get("min_feature_mm", 0)),
            "obstacle": "1",
            "obstacleFactor": "1",
            "obstacleType": "0",
            "scaleMax": "0",
            "scaleMin": "0",
            "scaleVisibility": "0",
            "unplacedVisibility": "0",
            "upsidedownLabels": "0",
            "zIndex": "0",
        },
    )
    settings.append(ddp("dd_properties"))
    return lab


# --- the detail themes ----------------------------------------------------------------------------
# Colours extend the established theme: ocean and inland water #07090c, land #0f0f0f with a white
# border, landuse as a barely-there lift, buildings one step above land, roads the only bright thing,
# and label text the one thing brighter still. min_z is the XYZ zoom the layer starts drawing at;
# below it QGIS does not even query the source, which is the entire point of the exercise.
#
# Order is draw order, top first: label-only layers, then boundaries, then roads, then the fills, and
# `land-base` above `ocean-base` at the bottom holding everything up. Labels are placed in a pass of
# their own after all geometry, so their position here decides which name wins a collision, not what
# is drawn over what.
#
# A theme is label-only when its renderer is `invisible_renderer` — QGIS will not load a vector layer
# without a renderer, so a layer that exists purely to carry text still has to declare one that draws
# nothing.
COUNTRY_LABEL = {
    "field": '"NAME"',
    "colour": "#d2d2d2",
    "size": 10,
    # Uppercase and letter-spaced: the convention that says "country" without needing a legend, and
    # it keeps country names from reading as just another big city.
    "capitalization": 1,
    "letter_spacing": 1.4,
    "halo_size": 1.0,
    "priority": 9,
    # In mm. A country too small to be worth 6 mm on screen loses its label until you zoom in far
    # enough, which stages the whole set by zoom without a single extra layer or scale rule.
    "min_feature_mm": 6,
}

STATE_LABEL = {
    "field": '"name"',
    "colour": "#8a8a8a",
    "size": 8,
    "halo_size": 0.8,
    "priority": 6,
    "min_feature_mm": 8,
}

OCEAN_LABEL = {
    "field": 'coalesce("name_en", "name")',
    "colour": "#5d7285",
    "size": 9,
    "italic": True,
    "capitalization": 1,
    "letter_spacing": 1.2,
    "halo_size": 0.8,
    "priority": 4,
}

MARINE_LABEL = {
    "field": 'coalesce("name_en", "name")',
    "colour": "#5d7285",
    "size": 8,
    "italic": True,
    "halo_size": 0.7,
    "priority": 4,
    "min_feature_mm": 5,
}


def marine_label_themes(prefix: str = "") -> list[dict]:
    """Natural Earth's useful low-zoom marine-label bands.

    Its rank metadata brings the seven oceans in at z1, major seas and bays at z2, and regional
    features at z4/z5. Ranks 4+ are deliberately left to OSM's `water-labels`, which starts at z6;
    carrying all 306 Natural Earth areas farther in would print the same bays from both sources.
    """
    return [
        {
            "name": f"{prefix}ocean-labels",
            "pg_table": "marine_areas",
            "geometry": "Polygon",
            "wkb": "MultiPolygon",
            "subset": '"featurecla" = \'ocean\'',
            "min_z": 1,
            "max_z": 6,
            "renderer": lambda: invisible_renderer("fill"),
            "label": OCEAN_LABEL,
        },
        {
            "name": f"{prefix}marine-labels-major",
            "pg_table": "marine_areas",
            "geometry": "Polygon",
            "wkb": "MultiPolygon",
            "subset": '"scalerank" = 1',
            "min_z": 2,
            "max_z": 5,
            "renderer": lambda: invisible_renderer("fill"),
            "label": MARINE_LABEL,
        },
        {
            "name": f"{prefix}marine-labels-regional",
            "pg_table": "marine_areas",
            "geometry": "Polygon",
            "wkb": "MultiPolygon",
            "subset": '"scalerank" = 2',
            "min_z": 4,
            "max_z": 5,
            "renderer": lambda: invisible_renderer("fill"),
            "label": MARINE_LABEL,
        },
        {
            "name": f"{prefix}marine-labels-local",
            "pg_table": "marine_areas",
            "geometry": "Polygon",
            "wkb": "MultiPolygon",
            "subset": '"scalerank" = 3',
            "min_z": 5,
            "max_z": 5,
            "renderer": lambda: invisible_renderer("fill"),
            "label": MARINE_LABEL,
        },
    ]


def ocean_base_theme(name: str) -> dict:
    return {
        "name": name,
        "pg_table": "ocean",
        "geometry": "Polygon",
        "wkb": "Polygon",
        "min_z": None,
        # The same-colour hairline overlaps the internal ST_Subdivide cuts. With no outline,
        # antialiasing can turn those shared edges into faint transparent seams.
        "renderer": lambda: single_renderer(
            symbol("fill", "0", [fill_layer("#07090c", "#07090c", 0.1)])
        ),
    }


def city_band(lo: int | None = None, hi: int | None = None) -> str:
    """A `place=city` subset for one population band, half-open: `lo <= population < hi`.

    `population` is an OSM free-text tag, so it is cast and coalesced: a non-numeric value casts to
    0 in SQLite and a missing one becomes 0, which puts both in the *smallest* band rather than
    dropping them off the map. Bands are exclusive, so no city is labelled twice once two of the
    tiers are visible at the same zoom.
    """
    pop = "coalesce(CAST(population AS INTEGER), 0)"
    clauses = ["place = 'city'"]
    if lo is not None:
        clauses.append(f"{pop} >= {lo}")
    if hi is not None:
        clauses.append(f"{pop} < {hi}")
    return " AND ".join(clauses)

THEMES = [
    {
        "name": "country-labels",
        "pg_table": "countries",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "min_z": None,
        "max_z": 9,
        "renderer": lambda: invisible_renderer("fill"),
        "label": COUNTRY_LABEL,
    },
    # Natural Earth ranks its own admin-1 units, so the tiers are its judgement, not a guess:
    # labelrank <= 4 is the ~800 units it considers worth a name on a world map, and 20 is the dump
    # for units it does not consider labellable at any scale, so those never appear at all.
    {
        "name": "state-labels-major",
        "pg_table": "states",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "subset": '"labelrank" <= 4',
        "min_z": 5,
        "max_z": 11,
        "renderer": lambda: invisible_renderer("fill"),
        "label": STATE_LABEL,
    },
    {
        "name": "state-labels",
        "pg_table": "states",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "subset": '"labelrank" > 4 AND "labelrank" < 20',
        "min_z": 7,
        "max_z": 11,
        "renderer": lambda: invisible_renderer("fill"),
        "label": STATE_LABEL,
    },
    # Cities in three tiers by population, because `place=city` alone is 11,906 points worldwide and
    # putting all of them on a z5 tile buries the continent. See city_band() for how the bands
    # handle OSM's unreliable population tag.
    {
        "name": "places-metro",
        "gpkg": "places",
        "geometry": "Point",
        "wkb": "Point",
        "subset": city_band(lo=1_000_000),
        "min_z": 4,
        # Cities keep a dot. The name alone floating over a dark field reads as a caption for
        # nothing; the dot is what makes it a place.
        "renderer": lambda: single_renderer(symbol("marker", "0", [marker_layer("#7a7a7a", 1.3)])),
        "label": {
            # OSM `name` is in the local script. name_en, where the mappers supplied it, keeps this
            # basemap readable to its English-speaking audience and sidesteps most missing glyphs.
            "field": 'coalesce("name_en", "name")',
            "colour": "#ededed",
            "size": 10,
            "halo_size": 1.0,
            "priority": 8,
        },
    },
    {
        "name": "places-city",
        "gpkg": "places",
        "geometry": "Point",
        "wkb": "Point",
        "subset": city_band(lo=200_000, hi=1_000_000),
        "min_z": 6,
        "renderer": lambda: single_renderer(symbol("marker", "0", [marker_layer("#6f6f6f", 1.1)])),
        "label": {
            "field": 'coalesce("name_en", "name")',
            "colour": "#e6e6e6",
            "size": 9,
            "halo_size": 0.9,
            "priority": 7,
        },
    },
    {
        "name": "places-city-minor",
        "gpkg": "places",
        "geometry": "Point",
        "wkb": "Point",
        "subset": city_band(hi=200_000),
        "min_z": 8,
        "renderer": lambda: single_renderer(symbol("marker", "0", [marker_layer("#636363", 1.0)])),
        "label": {
            "field": 'coalesce("name_en", "name")',
            "colour": "#d8d8d8",
            "size": 8.5,
            "halo_size": 0.9,
            "priority": 7,
        },
    },
    {
        "name": "places-town",
        "gpkg": "places",
        "geometry": "Point",
        "wkb": "Point",
        "subset": "place IN ('town', 'borough')",
        "min_z": 9,
        "renderer": lambda: single_renderer(symbol("marker", "0", [marker_layer("#575757", 0.8)])),
        "label": {
            "field": 'coalesce("name_en", "name")',
            "colour": "#b4b4b4",
            "size": 8,
            "halo_size": 0.8,
            "priority": 6,
        },
    },
    {
        "name": "places-village",
        "gpkg": "places",
        "geometry": "Point",
        "wkb": "Point",
        "subset": "place IN ('village', 'suburb')",
        "min_z": 12,
        "renderer": lambda: invisible_renderer("marker"),
        "label": {
            "field": 'coalesce("name_en", "name")',
            "colour": "#8f8f8f",
            "size": 7.5,
            "halo_size": 0.7,
            "priority": 3,
        },
    },
    {
        "name": "water-labels",
        "gpkg": "water",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        # water.gpkg already carries `name` — scripts/build-osm-detail.sh selected it into the
        # staging file — so lake and bay names cost no rebuild. The subset is what keeps this from
        # being a scan of an 8.5 GB layer: the overwhelming majority of water polygons are unnamed.
        "subset": "name IS NOT NULL",
        "min_z": 6,
        "renderer": lambda: invisible_renderer("fill"),
        "label": {
            "field": '"name"',
            "colour": "#5d7285",
            "size": 8,
            "italic": True,
            "halo_size": 0.7,
            "priority": 4,
            "min_feature_mm": 5,
        },
    },
    *marine_label_themes(),
    {
        "name": "states",
        "pg_table": "states",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "min_z": 4,
        # Outline only and dashed: dashed is what says "administrative boundary" rather than
        # "another street", and it stops a state border from reading as a motorway at the zooms
        # where both are on screen. The fill is switched off so the land colour underneath shows
        # through unchanged.
        #
        # Deliberately bright for a dark basemap: the same 0.5 mm as a motorway and a lighter grey
        # than one (#757575 against #4a4a4a). The state *name* only exists in the z5-11 band, so
        # from z12 up this line is the only cue left for which state you are in, and at 0.22 mm of
        # #3d3d3d on a #0f0f0f ground it was something you had to go looking for. A thin dashed
        # line also loses much of its nominal brightness to antialiasing — 0.35 mm of #666666
        # measured dimmer on screen than the roads it is supposed to out-rank.
        "renderer": lambda: single_renderer(
            symbol(
                "fill",
                "0",
                [fill_layer("#000000", "#757575", 0.5, style="no", outline_style="dash")],
            )
        ),
    },
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
        "pg_table": "countries",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "min_z": None,  # the land base of the detail group; always drawn
        "renderer": lambda: single_renderer(
            symbol("fill", "0", [fill_layer("#0f0f0f", "#ffffff", 0.26)])
        ),
    },
    ocean_base_theme("ocean-base"),
]

# The cheap route is a group too: ocean and marine labels cannot accompany a standalone WMTS
# layer. Its land and country-label layers intentionally use the same source and symbols as the
# detail group, so z3 -> z4 changes detail rather than changing the basemap underneath it.
SIMPLE_THEMES = [
    {
        "name": "simple-country-labels",
        "pg_table": "countries",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "min_z": None,
        "renderer": lambda: invisible_renderer("fill"),
        "label": COUNTRY_LABEL,
    },
    *marine_label_themes("simple-"),
    {
        "name": "simple-land-base",
        "pg_table": "countries",
        "geometry": "Polygon",
        "wkb": "MultiPolygon",
        "min_z": None,
        "renderer": lambda: single_renderer(
            symbol("fill", "0", [fill_layer("#0f0f0f", "#ffffff", 0.26)])
        ),
    },
    ocean_base_theme("simple-ocean-base"),
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


def theme_provider(theme: dict) -> str:
    """`postgres` for the Natural Earth themes, `ogr` for everything reading a GeoPackage."""
    return "postgres" if "pg_table" in theme else "ogr"


def pg_datasource(table: str, wkb: str) -> str:
    """A PostGIS layer reference carrying no credentials.

    `service=` is resolved from pg_service.conf, which qgis-server/entrypoint.sh writes at container
    start from the compose environment. That indirection is the whole reason project.qgz can live in
    git and in a published image: the file names a service, never a host, user or password.
    """
    return (
        f"service='mellabasemap' key='fid' srid=4326 type={wkb} "
        f'checkPrimaryKeyUnicity=\'1\' table="public"."{table}" (geom)'
    )


def theme_datasource(theme: dict) -> str:
    """Where a themed layer reads from.

    Most themes are GeoPackages under the /data bind mount. The Natural Earth themes are the
    exception: they read PostGIS, because OSM cannot supply a reliable land polygon here. GDAL's
    OSM driver fails to assemble the largest admin_level=2 boundary relations — measured on the full
    planet build, France, Spain, the USA and Russia all came out with no polygon at all (4 of 12
    sample cities had no land), emitting "Non closed ring detected" as it gave up. Those are exactly
    the countries with far-flung overseas territories or an antimeridian crossing. Natural Earth is
    generalized, so coastlines are coarser than OSM would be at z14+, but it is complete, and
    complete beats precise for the layer everything else is drawn on top of. The same argument moved
    states/provinces to Natural Earth: see scripts/load-natural-earth-states.sh.

    `subset` is how one source becomes several layers. Country names want to appear at different
    zooms depending on how important the country is, and QGIS's per-layer `minScale` is the staging
    mechanism this project already trusts — so rather than one layer with a rule-based label, the
    source is split by a provider-side filter and each slice gets its own `min_z`. The filter runs
    in PostGIS or in the GeoPackage's own SQLite, so it costs an index lookup, not a scan.
    """
    subset = theme.get("subset")
    if "pg_table" in theme:
        ds = pg_datasource(theme["pg_table"], theme["wkb"])
        # The postgres provider takes its filter as a trailing sql= clause.
        return f"{ds} sql={subset}" if subset else ds
    ds = f"/data/{theme['gpkg']}.gpkg|layername={theme['gpkg']}"
    # The OGR provider takes its filter as another pipe-separated token.
    return f"{ds}|subset={subset}" if subset else ds


def make_maplayer(theme: dict, layer_id: str) -> ET.Element:
    # `max_z` is the last zoom the layer draws at, and it exists for the label layers: a country
    # name is orientation at z6 and noise at z12, where you can see the roads it is sitting on.
    # Geometry layers leave it unset and draw all the way to z20.
    max_z = theme.get("max_z")
    scale_based = theme["min_z"] is not None or max_z is not None
    attrs = {
        "autoRefreshMode": "Disabled",
        "autoRefreshTime": "0",
        "geometry": theme["geometry"],
        "hasScaleBasedVisibilityFlag": "1" if scale_based else "0",
        "labelsEnabled": "1" if theme.get("label") else "0",
        "layerType": "Vector",
        "legendPlaceholderImage": "",
        # QGIS names these backwards from intuition: minScale is the zoomed-OUT bound (largest
        # denominator at which the layer still draws), maxScale the zoomed-in bound. 0 = unbounded.
        "maxScale": str(visibility_bound(max_z + 1)) if max_z is not None else "0",
        "minScale": str(visibility_bound(theme["min_z"])) if theme["min_z"] is not None else "0",
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
    ET.SubElement(ml, "provider", {"encoding": "UTF-8"}).text = theme_provider(theme)
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
    if theme.get("label"):
        ml.append(labeling(theme["label"], theme["geometry"]))
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


def prop_values(root: ET.Element, name: str) -> list[str]:
    el = prop(root, name)
    if el is None:
        raise SystemExit(f"patch-project: property {name!r} not found in project.qgs")
    return [v.text or "" for v in el.findall("value")]


def add_prop_value(root: ET.Element, name: str, value: str) -> None:
    values = prop_values(root, name)
    if value not in values:
        values.append(value)
        set_prop_values(root, name, values)


def remove_prop_values(root: ET.Element, name: str, removed: set[str]) -> None:
    set_prop_values(root, name, [v for v in prop_values(root, name) if v not in removed])


def replace_group(
    root: ET.Element,
    group_name: str,
    themes: list[dict],
    *,
    legacy_layer_names: set[str] | None = None,
) -> None:
    """Replace one managed group without disturbing unrelated project layers.

    Earlier code deleted every maplayer not referenced after removing `countries`. That happened to
    work while the project contained only one other layer, but it is unsafe now that both published
    products are groups. Track the ids owned by the group (and the old standalone low-zoom layer)
    explicitly, then remove only those ids from every parallel QGIS structure.
    """
    tree = root.find("layer-tree-group")
    legend = root.find("legend")
    layers = root.find("projectlayers")
    order = root.find("layerorder")
    if tree is None or legend is None or layers is None:
        raise SystemExit("patch-project: project.qgs is missing layer-tree-group/legend/projectlayers")

    removed_ids: set[str] = set()
    for grp in list(tree.findall("layer-tree-group")):
        if grp.get("name") == group_name:
            removed_ids.update(
                el.get("id") for el in grp.iter("layer-tree-layer") if el.get("id")
            )
            tree.remove(grp)
    for grp in list(legend.findall("legendgroup")):
        if grp.get("name") == group_name:
            legend.remove(grp)

    legacy_layer_names = legacy_layer_names or set()
    for el in list(tree.findall("layer-tree-layer")):
        if el.get("name") in legacy_layer_names:
            if el.get("id"):
                removed_ids.add(el.get("id"))
            tree.remove(el)
    for el in list(legend.findall("legendlayer")):
        if el.get("name") in legacy_layer_names:
            legend.remove(el)

    for ml in list(layers.findall("maplayer")):
        id_el = ml.find("id")
        if id_el is not None and id_el.text in removed_ids:
            layers.remove(ml)
    if order is not None:
        for item in list(order.findall("layer")):
            if item.get("id") in removed_ids:
                order.remove(item)
    custom_order = tree.find("custom-order")
    if custom_order is not None:
        for item in list(custom_order.findall("item")):
            if item.text in removed_ids:
                custom_order.remove(item)

    # A standalone layer is published by id, while a group is published by name. Remove the
    # obsolete ids and retain any unrelated WMTS publications the user may have added by hand.
    for path in ("WMTSLayers/Layer", "WMTSPngLayers/Layer"):
        remove_prop_values(root, path, removed_ids)

    group = ET.Element(
        "layer-tree-group",
        {"checked": "Qt::Checked", "expanded": "1", "groupLayer": "", "name": group_name},
    )
    legend_group = ET.Element(
        "legendgroup", {"checked": "Qt::Checked", "name": group_name, "open": "true"}
    )

    for theme in themes:
        layer_id = "{}_{}".format(theme["name"].replace("-", "_"), uuid.uuid4().hex)
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
                "providerKey": theme_provider(theme),
                "source": theme_datasource(theme),
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
        ET.SubElement(
            ET.SubElement(lg, "filegroup", {"hidden": "false", "open": "false"}),
            "legendlayerfile",
            {"isInOverview": "0", "layerid": layer_id, "visible": "1"},
        )
        layers.append(make_maplayer(theme, layer_id))
        if order is not None:
            order.append(ET.Element("layer", {"id": layer_id}))
        if custom_order is not None:
            ET.SubElement(custom_order, "item").text = layer_id

    # Top-level tree order is presentation order; the public routes ensure the two basemap groups
    # are never drawn together.
    tree.insert(0, group)
    legend.insert(0, legend_group)
    for path in ("WMTSLayers/Group", "WMTSPngLayers/Group"):
        add_prop_value(root, path, group_name)

    print(
        f"  added group {group_name!r} with {len(themes)} layers: "
        + ", ".join(t["name"] for t in themes)
    )
    print(f"  published group {group_name!r} for WMTS (png)")


def patch_base(root: ET.Element, require_data: bool) -> None:
    check_pg_tables(SIMPLE_THEMES, require_data)

    replace_group(
        root,
        "simple-countries",
        SIMPLE_THEMES,
        legacy_layer_names={"countries", "simple-countries"},
    )

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


def missing_pg_tables(themes: list[dict]) -> set[str] | None:
    """The PostGIS tables these themes need but the database does not have.

    Returns None when the check could not be made at all (no .env, no psql, database unreachable) —
    which is not the same answer as "nothing is missing", and the caller says so rather than
    claiming the project is fine.
    """
    env = REPO / ".env"
    if not env.exists() or shutil.which("psql") is None:
        return None
    # Read the keys we need rather than sourcing: .env holds unquoted values containing spaces.
    cfg = {}
    for line in env.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() in {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USERNAME", "DB_PASSWORD"}:
            cfg[key.strip()] = value
    if len(cfg) < 5:
        return None

    wanted = {t["pg_table"] for t in themes if "pg_table" in t}
    try:
        out = subprocess.run(
            ["psql", "-qtAX", "-h", cfg["DB_HOST"], "-p", cfg["DB_PORT"],
             "-U", cfg["DB_USERNAME"], "-d", cfg["DB_NAME"],
             "-c", "select tablename from pg_tables where schemaname='public'"],
            env={**os.environ, "PGPASSWORD": cfg["DB_PASSWORD"], "PGCONNECT_TIMEOUT": "5"},
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return wanted - set(out.stdout.split())


def check_pg_tables(themes: list[dict], require_data: bool) -> None:
    """Fail before writing a strict QGIS project that references absent PostGIS tables."""
    missing = missing_pg_tables(themes)
    if missing is None:
        print("  NOTE: could not reach PostgreSQL to verify the Natural Earth tables exist")
    elif missing and require_data:
        raise SystemExit(
            "patch-project: missing PostGIS table(s): " + ", ".join(sorted(missing)) + ".\n"
            "  Run scripts/load-natural-earth-ocean.sh and/or "
            "scripts/load-natural-earth-states.sh first, or pass --allow-missing-data to "
            "write the project anyway."
        )
    elif missing:
        print(f"  WARNING: referencing absent PostGIS table(s): {', '.join(sorted(missing))}")


def patch_detail(root: ET.Element, data_dir: Path, require_data: bool) -> None:
    # Only reference GeoPackages that actually exist. QGIS Server runs with
    # QGIS_SERVER_IGNORE_BAD_LAYERS=0, so a layer pointing at a missing file fails the entire
    # project rather than just that layer — and `buildings` is legitimately optional
    # (BUILDING_REGIONS in build-osm-detail.sh), so its absence is normal, not an error.
    #
    # The Natural Earth themes read PostGIS, not a file, so they are never in this check; their
    # tables are verified against the database below.
    gpkg_themes = [t for t in THEMES if "gpkg" in t]
    present = [t for t in THEMES if "gpkg" not in t or (data_dir / f"{t['gpkg']}.gpkg").exists()]
    absent = sorted({t["gpkg"] for t in gpkg_themes if t not in present})

    if not any("gpkg" in t for t in present):
        raise SystemExit(
            f"patch-project: no GeoPackages found in {data_dir}. "
            "Run scripts/build-osm-detail.sh first."
        )
    if absent and require_data:
        print(f"  skipping themes with no data: {', '.join(absent)}")
    elif absent:
        print(f"  WARNING: referencing absent GeoPackages: {', '.join(absent)}")
        present = list(THEMES)
    themes = present

    check_pg_tables(themes, require_data)
    replace_group(root, "countries", themes)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", action="store_true", help="build the Natural Earth low-zoom group and fix the WMTS pyramid depth")
    ap.add_argument("--detail", action="store_true", help="add the OSM detail layer group")
    ap.add_argument("--data-dir", default="/mnt/meow/OSM/out", help="where the GeoPackages live on the host")
    ap.add_argument("--allow-missing-data", action="store_true", help="write layers even when their PostGIS tables or GeoPackages are absent")
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
        patch_base(root, not args.allow_missing_data)
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
