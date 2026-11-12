"""Independent finish verification rejects geometry and appearance drift."""
import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_camera_finishes import check_glb, report_destination, verify_tree


BODY = "CameraModel/RealSense/RealSense_D405/meshes/glb/d405.glb"
PLUG = "CameraModel/RealSense/RealSense_D405/meshes/glb/plug.glb"
FINISH = {"profile": "assetsmodel-camera-materials-v1", "role": "satin_housing"}


def container(tree, binary=b"\0\0\0\0"):
    raw = json.dumps(tree, separators=(",", ":")).encode()
    raw += b" " * (-len(raw) % 4)
    return (struct.pack("<III", 0x46546C67, 2, 28 + len(raw) + len(binary))
            + struct.pack("<II", len(raw), 0x4E4F534A) + raw
            + struct.pack("<II", len(binary), 0x004E4942) + binary)


def sample(material=True):
    tree = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": 4}],
            "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}]}
    if material:
        tree["materials"] = [{"name": "Housing", "pbrMetallicRoughness": {
            "baseColorFactor": [0.5, 0.6, 0.7, 1], "metallicFactor": 0, "roughnessFactor": 1}}]
        tree["meshes"][0]["primitives"][0]["material"] = 0
    return tree


def finish(tree):
    result = copy.deepcopy(tree)
    result["materials"][0]["pbrMetallicRoughness"].update(metallicFactor=0.65, roughnessFactor=0.38)
    result["materials"][0]["extras"] = {"assetsmodel_camera_material": FINISH}
    return result


class CameraFinishVerificationTests(unittest.TestCase):
    def test_report_destination_protects_assets_and_requires_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "RobotModel/vendor/model/provenance.json"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"protected source metadata")
            alias = root / "report.json"
            alias.symlink_to(asset)
            for destination in (asset, alias, root / "ToolModel/new-report.json", root / "CameraModel/report.json"):
                with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "outside"):
                    report_destination(root, destination)
            with self.assertRaisesRegex(ValueError, "resolve to a .json"):
                report_destination(root, root / "report.glb")
            script = root / "protected.py"
            script.write_bytes(b"protected script")
            script_alias = root / "script.json"
            script_alias.symlink_to(script)
            with self.assertRaisesRegex(ValueError, "resolve to a .json"):
                report_destination(root, script_alias)
            safe = root / "docs/quality/report.json"
            self.assertEqual(report_destination(root, safe), safe)
            self.assertEqual(asset.read_bytes(), b"protected source metadata")
            self.assertFalse(safe.exists())

    def test_accepts_material_only_change(self):
        before = sample()
        report = check_glb(BODY, container(before), container(finish(before)))
        self.assertTrue(report["bin_identical"])
        self.assertEqual(report["added_triangle_material_bindings"], 0)
        self.assertEqual(report["material_changes"][0]["role"], "satin_housing")

    def test_rejects_binary_change_even_with_unchanged_json(self):
        before = sample()
        with self.assertRaisesRegex(ValueError, "BIN bytes changed"):
            check_glb(BODY, container(before), container(finish(before), b"\1\0\0\0"))

    def test_rejects_color_alpha_texture_and_name_changes(self):
        before = sample()
        for change in ("color", "alpha", "texture", "name"):
            with self.subTest(change=change):
                after = finish(before)
                material = after["materials"][0]
                if change == "color":
                    material["pbrMetallicRoughness"]["baseColorFactor"][0] = 0.4
                elif change == "alpha":
                    material["alphaMode"] = "BLEND"
                elif change == "texture":
                    material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
                else:
                    material["name"] = "Other"
                with self.assertRaisesRegex(ValueError, "Body material changed beyond"):
                    check_glb(BODY, container(before), container(after))

    def test_rejects_nonfinite_or_boolean_finish_factor(self):
        before = sample()
        for factor in (float("nan"), float("inf"), -0.1, 1.1, True):
            with self.subTest(factor=factor):
                after = finish(before)
                after["materials"][0]["pbrMetallicRoughness"]["roughnessFactor"] = factor
                with self.assertRaisesRegex(ValueError, "finite numbers"):
                    check_glb(BODY, container(before), container(after))

    def test_rejects_wrong_role_profile_and_unplanned_finish(self):
        before = sample()
        for change in ("profile", "role", "roughness"):
            with self.subTest(change=change):
                after = finish(before)
                # Copy metadata so this rejection fixture cannot mutate FINISH.
                material = after["materials"][0]
                material["extras"]["assetsmodel_camera_material"] = dict(FINISH)
                if change == "roughness":
                    material["pbrMetallicRoughness"]["roughnessFactor"] = 0.9
                else:
                    material["extras"]["assetsmodel_camera_material"][change] = "unplanned"
                with self.assertRaisesRegex(ValueError, "unexpected|Unexpected"):
                    check_glb(BODY, container(before), container(after))

    def test_rejects_node_or_material_binding_change(self):
        before = sample()
        for key in ("node", "binding"):
            with self.subTest(key=key):
                after = finish(before)
                if key == "node":
                    after["nodes"][0]["scale"] = [2, 2, 2]
                else:
                    after["meshes"][0]["primitives"][0]["material"] = 1
                with self.assertRaisesRegex(ValueError, "Non-material glTF JSON changed"):
                    check_glb(BODY, container(before), container(after))

    def plug(self):
        before = sample(False)
        before["meshes"][0]["primitives"].append({"mode": 1, "attributes": {"POSITION": 0}, "indices": 1})
        after = copy.deepcopy(before)
        after["materials"] = [{"name": "Camera USB plug graphite polymer", "pbrMetallicRoughness": {
            "baseColorFactor": [0.01938236, 0.02415763, 0.02955683, 1.0],
            "metallicFactor": 0.0, "roughnessFactor": 0.55},
            "extras": {"assetsmodel_camera_material": {**FINISH, "role": "plug_polymer"}}}]
        after["meshes"][0]["primitives"][0]["material"] = 0
        return before, after

    def test_plug_adds_one_triangle_binding_and_preserves_guides(self):
        before, after = self.plug()
        report = check_glb(PLUG, container(before), container(after))
        self.assertEqual(report["added_triangle_material_bindings"], 1)
        self.assertEqual(report["material_changes"][0]["role"], "plug_polymer")
        after["meshes"][0]["primitives"][1]["material"] = 0
        with self.assertRaisesRegex(ValueError, "Non-material glTF JSON changed"):
            check_glb(PLUG, container(before), container(after))

    def test_plug_rejects_missing_binding_or_metal(self):
        before, original = self.plug()
        for mode in ("missing-binding", "metal"):
            with self.subTest(mode=mode):
                after = copy.deepcopy(original)
                if mode == "missing-binding":
                    del after["meshes"][0]["primitives"][0]["material"]
                else:
                    after["materials"][0]["pbrMetallicRoughness"]["metallicFactor"] = 1
                with self.assertRaises(ValueError):
                    check_glb(PLUG, container(before), container(after))

    def test_rejects_offscope_edits_additions_and_deletions_in_real_git_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            paths = ("RobotModel/vendor/arm/arm.urdf", "CameraModel/camera/meshes/stl/collision.stl")
            for relative in paths:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"original")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Verification test", "-c",
                            "user.email=test@example.invalid", "commit", "-qm", "baseline"], check=True)
            self.assertFalse(verify_tree(root, "HEAD")["errors"])
            (root / paths[0]).write_bytes(b"modified")
            (root / paths[1]).unlink()
            (root / "RobotModel/vendor/arm/new.glb").write_bytes(b"added")
            report = verify_tree(root, "HEAD")
            self.assertEqual(report["summary"]["errors"], 3)
            self.assertEqual({row["error"] for row in report["errors"]},
                             {"Asset file removed", "Asset file added", "Off-scope asset bytes changed"})


if __name__ == "__main__":
    unittest.main()
