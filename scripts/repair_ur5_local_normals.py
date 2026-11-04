"""Reproduce the reviewed, local normal repair for two legacy UR5 CB3 meshes.

This is deliberately source-SHA and corner-mask locked. It does not discover
new smoothing candidates or overwrite repository assets. See the diagnosis and
explicit mask in docs/quality/ur5-*.json before considering adoption.

python scripts/repair_ur5_local_normals.py \
  --source-model RobotModel/UniversalRobots/UR5 \
  --output-model /tmp/ur5-local-normal-review/RobotModel/UniversalRobots/UR5
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
from pathlib import Path

import numpy as np

from glb_format import read_glb, validate_glb
from prepare_preview import accessor_array
from refine_materials import encode_glb
from refine_visual_normals import prune_accessors
from smooth_oriented_sheets import smooth_oriented_normals

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    "Visual/glb/wrist1.glb": "39e646c1b18015cbcb775366b5e8aafcd213eccbc144b99a147c1fb70a9f4759",
    "Visual/glb/shoulder.glb": "a061185c1ca792cd98637aabda48090c512b29fc824ea177b93e2f6531d618d6",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def repair(source, mask):
    """Return a review GLB; preserve all ordered geometry and unmasked normals."""
    tree, binary = read_glb(source)
    target, packed = copy.deepcopy(tree), bytearray(binary)
    selected = {(p["mesh"], p["primitive"]): p for p in mask["primitives"]}
    if len(selected) != len(mask["primitives"]):
        raise ValueError("Duplicate primitive selection")

    def append(values, component, kind, position=False):
        values = np.ascontiguousarray(values)
        packed.extend(b"\0" * (-len(packed) % 4))
        vi = len(target["bufferViews"])
        target["bufferViews"].append({"buffer": 0, "byteOffset": len(packed), "byteLength": values.nbytes})
        packed.extend(values.tobytes())
        ai = len(target["accessors"])
        accessor = {"bufferView": vi, "componentType": component, "type": kind, "count": len(values)}
        if position:
            accessor.update(min=values.min(axis=0).tolist(), max=values.max(axis=0).tolist())
        target["accessors"].append(accessor)
        return ai

    records = []
    for (mi, pi), entry in selected.items():
        primitive = tree["meshes"][mi]["primitives"][pi]
        if primitive.get("mode", 4) != 4 or set(primitive["attributes"]) != {"POSITION", "NORMAL"}:
            raise ValueError("Expected the reviewed indexed POSITION/NORMAL triangles")
        material = tree["materials"][primitive["material"]]["name"]
        if material != entry["material_name"] or not any(x in material for x in ("verbindung", "blau")):
            raise ValueError("Material is outside the reviewed gray/blue regions")
        vertices = accessor_array(tree, binary, primitive["attributes"]["POSITION"])
        faces = accessor_array(tree, binary, primitive["indices"]).reshape(-1, 3)
        original = accessor_array(tree, binary, primitive["attributes"]["NORMAL"])[faces].reshape(-1, 3)
        if len(original) != entry["corner_count"] or sha(original.tobytes()) != entry["source_corner_normals_sha256"]:
            raise ValueError("Source ordered normal field differs from the reviewed mask")
        indices = np.asarray(entry["selected_corner_indices"], dtype=np.int64)
        if len(indices) != entry["selected_corner_count"] or not len(indices):
            raise ValueError("Empty or inconsistent selected corner count")
        if indices[0] < 0 or indices[-1] >= len(original) or np.any(np.diff(indices) <= 0):
            raise ValueError("Selected corner addresses must be unique, ascending, and in range")
        chosen = np.zeros(len(original), dtype=bool)
        chosen[indices] = True
        normals = original.copy()
        # Computing a target does not authorize applying it outside the reviewed
        # exact mask. Existing smooth normals elsewhere remain byte identical.
        normals[chosen] = smooth_oriented_normals(vertices, faces, angle=30)[chosen].astype("<f4")
        if normals[~chosen].tobytes() != original[~chosen].tobytes():
            raise ValueError("An unselected authored normal changed")
        if sha(normals.tobytes()) != entry["expected_output_corner_normals_sha256"]:
            raise ValueError("Target algorithm differs from the visually reviewed normal field")

        # Exact float32 tuple deduplication only. No decimal quantization, weld
        # tolerance, triangle reordering, or normal averaging occurs here.
        tuples = np.ascontiguousarray(np.column_stack((vertices[faces].reshape(-1, 3), normals)), dtype="<f4")
        opaque = tuples.view(np.dtype((np.void, 24))).reshape(-1)
        _, first, inverse = np.unique(opaque, return_index=True, return_inverse=True)
        complete = tuples[first]
        new_faces = inverse.reshape(-1, 3)
        if complete[new_faces, :3].tobytes() != vertices[faces].tobytes():
            raise ValueError("Tuple compaction changed ordered triangle positions")
        new_primitive = target["meshes"][mi]["primitives"][pi]
        new_primitive["attributes"]["POSITION"] = append(complete[:, :3], 5126, "VEC3", position=True)
        new_primitive["attributes"]["NORMAL"] = append(complete[:, 3:], 5126, "VEC3")
        dtype, component = ("<u2", 5123) if len(complete) <= 65536 else ("<u4", 5125)
        new_primitive["indices"] = append(new_faces.astype(dtype).reshape(-1), component, "SCALAR")
        records.append({"mesh": mi, "primitive": pi, "material_name": material,
                        "changed_normal_corners": int(chosen.sum()), "total_corners": len(original),
                        "unselected_corner_normals_byte_exact": True})

    target["buffers"][0]["byteLength"] = len(packed)
    packed.extend(b"\0" * (-len(packed) % 4))
    target, new_binary = prune_accessors(target, bytes(packed))
    # Verify the emitted representation, including entirely untouched groups.
    for key in set(tree) | set(target):
        if key not in {"meshes", "accessors", "bufferViews", "buffers"} and tree.get(key) != target.get(key):
            raise ValueError(f"Root metadata changed: {key}")
    for mi, (before, after) in enumerate(zip(tree["meshes"], target["meshes"])):
        if {k: v for k, v in before.items() if k != "primitives"} != {k: v for k, v in after.items() if k != "primitives"}:
            raise ValueError("Mesh metadata changed")
        for pi, (old, new) in enumerate(zip(before["primitives"], after["primitives"])):
            if {k: v for k, v in old.items() if k not in {"attributes", "indices"}} != {
                k: v for k, v in new.items() if k not in {"attributes", "indices"}
            }:
                raise ValueError("Primitive material or mode metadata changed")
            oi = accessor_array(tree, binary, old["indices"]).reshape(-1)
            ni = accessor_array(target, new_binary, new["indices"]).reshape(-1)
            for semantic, ai in old["attributes"].items():
                a = accessor_array(tree, binary, ai)[oi]
                b = accessor_array(target, new_binary, new["attributes"][semantic])[ni]
                if semantic == "NORMAL" and (mi, pi) in selected:
                    entry = selected[(mi, pi)]
                    keep = np.ones(len(a), dtype=bool)
                    keep[entry["selected_corner_indices"]] = False
                    if a[keep].tobytes() != b[keep].tobytes() or sha(b.tobytes()) != entry["expected_output_corner_normals_sha256"]:
                        raise ValueError("Emitted local normal mask differs")
                elif a.tobytes() != b.tobytes():
                    raise ValueError(f"Emitted corner attribute changed: {semantic}")
    raw = encode_glb(target, new_binary)
    validate_glb(raw)
    return raw, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--output-model", type=Path, required=True)
    parser.add_argument("--mask", type=Path, default=ROOT / "docs/quality/ur5-local-normal-mask.json")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source, output = args.source_model.resolve(), args.output_model.resolve()
    if output.is_relative_to(ROOT) or output.is_relative_to(source) or source.is_relative_to(output):
        parser.error("Review output must be outside the repository and source model")
    if output.exists() and any(output.iterdir()):
        parser.error("Review output must be a new or empty directory")
    if not (source / "UR5.urdf").is_file():
        parser.error("Source must be the reviewed legacy UR5 model directory")
    mask_bytes = args.mask.read_bytes()
    mask = json.loads(mask_bytes)
    if mask.get("schema") != 1 or len(mask["files"]) != len(ALLOWED) or {
        r["path"]: r["source_sha256"] for r in mask["files"]
    } != ALLOWED:
        parser.error("Mask must cover exactly the two reviewed source-SHA-locked UR5 files")
    report_path = (args.report or output / "ur5-local-normal-repair.json").resolve()
    if report_path.suffix != ".json" or report_path.is_relative_to(source) or any(
        report_path.is_relative_to(ROOT / group) for group in ("RobotModel", "ToolModel", "CameraModel")
    ) or report_path == args.mask.resolve():
        parser.error("Report must be JSON outside source assets and must not overwrite the mask")
    results, outputs = [], {}
    for entry in mask["files"]:
        original = (source / entry["path"]).read_bytes()
        if sha(original) != ALLOWED[entry["path"]]:
            parser.error(f"Source SHA changed; this mask cannot be generalized: {entry['path']}")
        raw, records = repair(original, entry)
        outputs[entry["path"]] = raw
        results.append({"path": entry["path"], "source_sha256": sha(original), "output_sha256": sha(raw),
                        "source_bytes": len(original), "output_bytes": len(raw), "primitives": records,
                        "ordered_geometry_and_winding_byte_exact": True, "material_node_scene_exact": True})
    shutil.copytree(source, output, dirs_exist_ok=True)
    for relative, raw in outputs.items():
        destination = output / relative
        pending = destination.with_suffix(".pending")
        pending.write_bytes(raw)
        pending.replace(destination)
    report = {"trial_only": True, "source_model": str(source), "output_model": str(output),
              "mask_path": str(args.mask.resolve()), "mask_sha256": sha(mask_bytes),
              "script_sha256": sha(Path(__file__).read_bytes()), "files": results}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"files": len(results), "changed_normal_corners": sum(p["changed_normal_corners"] for r in results for p in r["primitives"]),
                      "source_bytes": sum(r["source_bytes"] for r in results), "output_bytes": sum(r["output_bytes"] for r in results)}))


if __name__ == "__main__":
    main()
