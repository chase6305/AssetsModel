"""Independently verify a camera finish pass against a committed asset baseline.

The checker uses only the standard library and its own GLB container reader.
It compares every file under the three asset categories, including untracked
additions. Only six RealSense bodies and four visual plugs may differ.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess


CATEGORIES = ("CameraModel", "RobotModel", "ToolModel")
MODELS = ("D405", "D415", "D435", "D436", "D455", "D585")
BODY_PATHS = frozenset(
    f"CameraModel/RealSense/RealSense_{model}/meshes/glb/{model.lower()}.glb"
    for model in MODELS
)
PLUG_PATHS = frozenset(
    f"CameraModel/RealSense/RealSense_{model}/meshes/glb/plug.glb"
    for model in MODELS[:4]
)
ALLOWED_PATHS = BODY_PATHS | PLUG_PATHS
MARKER = "assetsmodel_camera_material"
# Kept as independent acceptance criteria, not imported from the transformer.
PROFILE = "assetsmodel-camera-materials-v1"
HOUSING_FACTORS = {"metallicFactor": 0.65, "roughnessFactor": 0.38}
PLUG_MATERIAL = {
    "name": "Camera USB plug graphite polymer",
    "pbrMetallicRoughness": {
        "baseColorFactor": [0.01938236, 0.02415763, 0.02955683, 1.0],
        "metallicFactor": 0.0, "roughnessFactor": 0.55,
    },
    "extras": {MARKER: {"profile": PROFILE, "role": "plug_polymer"}},
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_container(data):
    """Decode the two embedded GLB chunks without production GLB helpers."""
    if len(data) < 28 or struct.unpack_from("<III", data) != (0x46546C67, 2, len(data)):
        raise ValueError("Invalid GLB header")
    chunks, offset = [], 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("Truncated GLB chunk header")
        length, kind = struct.unpack_from("<II", data, offset)
        offset += 8
        if length % 4 or offset + length > len(data):
            raise ValueError("Invalid GLB chunk length")
        chunks.append((kind, data[offset:offset + length]))
        offset += length
    if [kind for kind, _ in chunks] != [0x4E4F534A, 0x004E4942]:
        raise ValueError("Expected exactly JSON and embedded BIN chunks")
    tree, binary = json.loads(chunks[0][1]), chunks[1][1]
    if tree.get("asset", {}).get("version") != "2.0":
        raise ValueError("Expected glTF 2.0")
    buffers = tree.get("buffers", [])
    if (len(buffers) != 1 or "uri" in buffers[0]
            or not 0 <= len(binary) - buffers[0].get("byteLength", -100) <= 3):
        raise ValueError("Expected one valid embedded buffer")
    return tree, binary


def finite_factor(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def check_finish(material, role):
    pbr = material.get("pbrMetallicRoughness", {})
    if not all(finite_factor(pbr.get(key)) for key in ("metallicFactor", "roughnessFactor")):
        raise ValueError("Finish factors must be finite numbers in [0, 1]")
    metadata = material.get("extras", {}).get(MARKER)
    if metadata != {"profile": PROFILE, "role": role}:
        raise ValueError("Changed material has an unexpected finish profile or role")
    return metadata


def without_finish(material):
    result = copy.deepcopy(material)
    pbr = result.get("pbrMetallicRoughness", {})
    for key in ("metallicFactor", "roughnessFactor"):
        pbr.pop(key, None)
    if not pbr:
        result.pop("pbrMetallicRoughness", None)
    extras = result.get("extras", {})
    extras.pop(MARKER, None)
    if not extras:
        result.pop("extras", None)
    return result


def check_glb(path, before_data, after_data):
    if path not in ALLOWED_PATHS:
        raise ValueError("GLB is outside the camera finish allowlist")
    before, old_binary = read_container(before_data)
    after, binary = read_container(after_data)
    if old_binary != binary:
        raise ValueError("Embedded BIN bytes changed")
    old_tree, new_tree = copy.deepcopy(before), copy.deepcopy(after)
    old_materials, new_materials = old_tree.pop("materials", []), new_tree.pop("materials", [])
    edits, bindings = [], 0
    if path in BODY_PATHS:
        if not old_materials or len(old_materials) != len(new_materials):
            raise ValueError("Body material array length changed")
        used = {primitive.get("material") for mesh in before.get("meshes", [])
                for primitive in mesh.get("primitives", []) if primitive.get("mode", 4) == 4}
        for index, (old, new) in enumerate(zip(old_materials, new_materials)):
            if old != new:
                if index not in used:
                    raise ValueError("Unused body material changed")
                metadata = check_finish(new, "satin_housing")
                if any(new.get("pbrMetallicRoughness", {}).get(key) != value
                       for key, value in HOUSING_FACTORS.items()):
                    raise ValueError("Unexpected camera housing finish factors")
                if without_finish(old) != without_finish(new):
                    raise ValueError("Body material changed beyond roughness, metallic and finish metadata")
                edits.append({"index": index, "name": new.get("name"), "role": metadata["role"],
                              "before": old.get("pbrMetallicRoughness", {}),
                              "after": new.get("pbrMetallicRoughness", {})})
    else:
        if old_materials or len(new_materials) != 1:
            raise ValueError("Plug must add exactly one material to an unmaterialed GLB")
        material = new_materials[0]
        metadata = check_finish(material, "plug_polymer")
        pbr = material["pbrMetallicRoughness"]
        rgba = pbr.get("baseColorFactor", [])
        if (set(pbr) != {"baseColorFactor", "metallicFactor", "roughnessFactor"}
                or len(rgba) != 4 or not all(finite_factor(v) for v in rgba)
                or rgba[3] != 1 or max(rgba[:3]) > 0.15 or pbr["metallicFactor"] != 0):
            raise ValueError("Plug requires an opaque nonmetal graphite material")
        if material != PLUG_MATERIAL:
            raise ValueError("Unexpected graphite plug material")
        if len(old_tree.get("meshes", [])) != len(new_tree.get("meshes", [])):
            raise ValueError("Plug mesh count changed")
        for old_mesh, new_mesh in zip(old_tree.get("meshes", []), new_tree.get("meshes", [])):
            if len(old_mesh.get("primitives", [])) != len(new_mesh.get("primitives", [])):
                raise ValueError("Plug primitive count changed")
            for old_primitive, new_primitive in zip(old_mesh.get("primitives", []), new_mesh.get("primitives", [])):
                if "material" in old_primitive:
                    raise ValueError("Baseline plug already has material bindings")
                if old_primitive.get("mode", 4) == 4:
                    if new_primitive.pop("material", None) != 0:
                        raise ValueError("Plug triangle does not use the sole added material")
                    bindings += 1
        if not bindings:
            raise ValueError("Plug contains no triangle material bindings")
        edits.append({"index": 0, "name": material.get("name"), "role": metadata["role"], "after": pbr})
    if old_tree != new_tree:
        raise ValueError("Non-material glTF JSON changed")
    return {"path": path, "before_sha256": digest(before_data), "after_sha256": digest(after_data),
            "bin_sha256": digest(binary), "bin_identical": True, "non_material_json_identical": True,
            "added_triangle_material_bindings": bindings, "material_changes": edits}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def verify_tree(root, baseline_ref):
    root = Path(root).resolve()
    baseline = git(root, "rev-parse", "--verify", "--end-of-options", baseline_ref + "^{commit}").decode().strip()
    entries = {}
    for row in git(root, "ls-tree", "-r", "-z", "--full-tree", baseline, "--", *CATEGORIES).split(b"\0"):
        if row:
            header, raw_path = row.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            if kind != "blob" or mode not in ("100644", "100755"):
                raise ValueError("Asset baseline must contain ordinary files")
            entries[raw_path.decode()] = oid
    if not entries:
        raise ValueError("Baseline contains no tracked asset files")
    current, symlinks = set(), []
    for category in CATEGORIES:
        for path in (root / category).rglob("*"):
            if path.is_symlink():
                symlinks.append(path.relative_to(root).as_posix())
            elif path.is_file():
                current.add(path.relative_to(root).as_posix())
    errors = [{"path": path, "error": "Asset symlinks are not allowed"} for path in symlinks]
    errors.extend({"path": path, "error": "Asset file removed"} for path in sorted(set(entries) - current))
    errors.extend({"path": path, "error": "Asset file added"} for path in sorted(current - set(entries)))
    changed, unchanged = [], 0
    # Stream one blob at a time. Avoid loading the entire asset baseline into RAM.
    process = subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for path, oid in sorted(entries.items()):
            if path not in current:
                continue
            process.stdin.write((oid + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline().decode().strip().split()
            if len(header) != 3 or header[1] != "blob":
                raise ValueError("Unable to read baseline blob")
            original = process.stdout.read(int(header[2]))
            if process.stdout.read(1) != b"\n":
                raise ValueError("Truncated baseline blob stream")
            actual = (root / path).read_bytes()
            if original == actual:
                unchanged += 1
            elif path not in ALLOWED_PATHS:
                errors.append({"path": path, "error": "Off-scope asset bytes changed"})
            else:
                try:
                    changed.append(check_glb(path, original, actual))
                except (ValueError, TypeError, KeyError, IndexError, struct.error) as exc:
                    errors.append({"path": path, "error": str(exc)})
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    return {"summary": {"baseline_ref": baseline_ref, "baseline_commit": baseline,
                        "baseline_asset_files": len(entries), "unchanged_asset_files": unchanged,
                        "verified_camera_changes": len(changed),
                        "verified_material_roles": dict(Counter(edit["role"] for row in changed
                                                                for edit in row["material_changes"])),
                        "errors": len(errors)},
            "scope": "All files under CameraModel, RobotModel and ToolModel; only ten camera GLBs may change",
            "verified_changes": changed, "errors": errors}


def report_destination(root, output):
    """Resolve the report path without allowing a write into any asset tree."""
    root, output = Path(root).resolve(), Path(output)
    destination = output.resolve()
    if any(destination.is_relative_to((root / category).resolve()) for category in CATEGORIES):
        raise ValueError("Report must be outside CameraModel, RobotModel and ToolModel")
    if output.suffix.lower() != ".json" or destination.suffix.lower() != ".json":
        raise ValueError("Report path must resolve to a .json file")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    try:
        destination = report_destination(args.root, args.json)
    except ValueError as exc:
        parser.error(str(exc))
    report = verify_tree(args.root, args.baseline_ref)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report["summary"]))
    return int(bool(report["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
