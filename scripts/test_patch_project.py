#!/usr/bin/env python3
"""Structural regression tests for the reviewable QGIS project patcher."""
from __future__ import annotations

import importlib.util
import io
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
PATCHER_PATH = REPO / "scripts" / "patch-project.py"
PROJECT = REPO / "qgis-server" / "project" / "project.qgz"

spec = importlib.util.spec_from_file_location("patch_project", PATCHER_PATH)
assert spec is not None and spec.loader is not None
patch_project = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch_project)


def load_root():
    with zipfile.ZipFile(PROJECT) as archive:
        qgs_name = next(name for name in archive.namelist() if name.endswith(".qgs"))
        return patch_project.ET.fromstring(archive.read(qgs_name))


def property_values(root, path: str) -> list[str]:
    return [value.text or "" for value in patch_project.prop(root, path).findall("value")]


class ProjectGroupsTest(unittest.TestCase):
    def patched_root(self):
        root = load_root()
        # Run twice: structural idempotence means replacement, never accumulation. UUIDs may change.
        with redirect_stdout(io.StringIO()):
            for _ in range(2):
                patch_project.replace_group(
                    root,
                    "simple-countries",
                    patch_project.SIMPLE_THEMES,
                    legacy_layer_names={"countries", "simple-countries"},
                )
                patch_project.replace_group(root, "countries", patch_project.THEMES)
        return root

    def test_both_public_names_are_groups_and_no_standalone_layer_survives(self):
        root = self.patched_root()
        tree = root.find("layer-tree-group")
        groups = tree.findall("layer-tree-group")

        self.assertEqual([g.get("name") for g in groups].count("simple-countries"), 1)
        self.assertEqual([g.get("name") for g in groups].count("countries"), 1)
        self.assertFalse(tree.findall("layer-tree-layer"))
        self.assertCountEqual(
            property_values(root, "WMTSLayers/Group"), ["simple-countries", "countries"]
        )
        self.assertCountEqual(
            property_values(root, "WMTSPngLayers/Group"), ["simple-countries", "countries"]
        )
        self.assertEqual(property_values(root, "WMTSLayers/Layer"), [])
        self.assertEqual(property_values(root, "WMTSPngLayers/Layer"), [])

    def test_group_order_puts_ocean_beneath_land(self):
        root = self.patched_root()
        groups = {
            group.get("name"): [layer.get("name") for layer in group.findall("layer-tree-layer")]
            for group in root.find("layer-tree-group").findall("layer-tree-group")
        }

        self.assertEqual(groups["simple-countries"][-2:], ["simple-land-base", "simple-ocean-base"])
        self.assertEqual(groups["countries"][-2:], ["land-base", "ocean-base"])
        self.assertIn("simple-ocean-labels", groups["simple-countries"])
        self.assertIn("ocean-labels", groups["countries"])

    def test_every_generated_layer_is_referenced_once(self):
        root = self.patched_root()
        tree_ids = [layer.get("id") for layer in root.find("layer-tree-group").iter("layer-tree-layer")]
        map_ids = [layer.findtext("id") for layer in root.find("projectlayers").findall("maplayer")]
        order_ids = [layer.get("id") for layer in root.find("layerorder").findall("layer")]

        self.assertEqual(len(tree_ids), len(set(tree_ids)))
        self.assertCountEqual(map_ids, tree_ids)
        self.assertCountEqual(order_ids, tree_ids)

    def test_marine_label_zoom_bands_are_encoded_on_layers(self):
        root = self.patched_root()
        layers = {
            layer.findtext("layername"): layer
            for layer in root.find("projectlayers").findall("maplayer")
        }
        expected = {
            "ocean-labels": (1, 6),
            "marine-labels-major": (2, 5),
            "marine-labels-regional": (4, 5),
            "marine-labels-local": (5, 5),
        }
        for name, (min_z, max_z) in expected.items():
            layer = layers[name]
            self.assertEqual(layer.get("minScale"), str(patch_project.visibility_bound(min_z)))
            self.assertEqual(layer.get("maxScale"), str(patch_project.visibility_bound(max_z + 1)))
            self.assertIn('table="public"."marine_areas"', layer.findtext("datasource"))


if __name__ == "__main__":
    unittest.main()
