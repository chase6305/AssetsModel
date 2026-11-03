"""Audit GLB shading and per-URDF rendering budgets without changing assets.

Requires NumPy from requirements-mesh.txt. Warnings identify review candidates;
triangle budgets and broad material choices are observations, not aesthetic scores.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

from glb_format import accessor_bytes, read_glb, validate_glb

CATEGORIES = ("CameraModel", "RobotModel", "ToolModel")
DTYPES = {5120: "i1", 5121: "u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}


def array(tree, binary, index):
    raw, _, width = accessor_bytes(tree, binary, index)
    accessor = tree["accessors"][index]
    values = np.frombuffer(raw, dtype=DTYPES[accessor["componentType"]]).reshape(-1, width)
    if accessor.get("normalized", False):
        dtype = values.dtype
        if dtype.kind == "u":
            values = values.astype(np.float64) / np.iinfo(dtype).max
        elif dtype.kind == "i":
            values = np.maximum(values.astype(np.float64) / np.iinfo(dtype).max, -1.0)
    return values


def scene_meshes(tree):
    """Yield mesh instances reachable from the active scene, excluding CAD guides."""
    scenes = tree.get("scenes", [])
    if not scenes:
        raise ValueError("GLB has no scene")

    def walk(index, ancestors):
        if index in ancestors:
            raise ValueError("Cycle in scene nodes")
        node = tree["nodes"][index]
        if "mesh" in node:
            yield node["mesh"]
        for child in node.get("children", []):
            yield from walk(child, ancestors | {index})

    for node in scenes[tree.get("scene", 0)].get("nodes", []):
        yield from walk(node, set())


def audit_glb(path, roles=()):
    """Inspect stored shading and count active-scene draw instances separately."""
    payload = Path(path).read_bytes()
    tree, binary = read_glb(payload)
    container = validate_glb(payload)
    instances = Counter(scene_meshes(tree))
    errors, warnings = [], []
    result = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
              "roles": sorted(roles),
              "stored_triangles": container["faces"], "line_segments": container["lines"],
              "default_scene_triangles": 0, "default_scene_primitives": 0,
              "materials": len(tree.get("materials", [])), "normal_vertices": 0,
              "invalid_normal_vertices": 0, "missing_visual_normal_primitives": 0,
              "metallic_materials": 0, "double_sided_materials": 0,
              "transparent_materials": 0}
    for index, material in enumerate(tree.get("materials", [])):
        pbr = material.get("pbrMetallicRoughness", {})
        factor_fields = [("baseColorFactor", pbr.get("baseColorFactor", [1, 1, 1, 1]), 4),
                         ("metallicFactor", [pbr.get("metallicFactor", 1)], 1),
                         ("roughnessFactor", [pbr.get("roughnessFactor", 1)], 1),
                         ("emissiveFactor", material.get("emissiveFactor", [0, 0, 0]), 3)]
        for field, values, width in factor_fields:
            values = np.asarray(values, dtype=float)
            if values.shape != (width,) or not np.isfinite(values).all() or np.any((values < 0) | (values > 1)):
                errors.append(f"material {index}: {field} must contain {width} finite values in [0, 1]")
        result["metallic_materials"] += int(float(pbr.get("metallicFactor", 1)) > 0)
        result["double_sided_materials"] += int(bool(material.get("doubleSided", False)))
        alpha_mode = material.get("alphaMode", "OPAQUE")
        result["transparent_materials"] += int(alpha_mode != "OPAQUE")
        color = pbr.get("baseColorFactor", [1, 1, 1, 1])
        if len(color) == 4 and color[3] < 1 and alpha_mode == "OPAQUE":
            warnings.append(f"material {index}: base color alpha is ignored by OPAQUE mode")

    for mesh_index, mesh in enumerate(tree.get("meshes", [])):
        uses = instances[mesh_index]
        for primitive_index, primitive in enumerate(mesh["primitives"]):
            if primitive.get("mode", 4) != 4:
                continue
            label = f"mesh {mesh_index} primitive {primitive_index}"
            count = tree["accessors"][primitive["indices"]]["count"] // 3
            result["default_scene_triangles"] += count * uses
            result["default_scene_primitives"] += uses
            attributes = primitive["attributes"]
            vertices = array(tree, binary, attributes["POSITION"])
            if not np.isfinite(vertices).all():
                errors.append(f"{label}: non-finite positions")
            if "NORMAL" not in attributes:
                if uses and "visual" in roles:
                    result["missing_visual_normal_primitives"] += 1
                    warnings.append(f"{label}: visual surface has no stored normals")
                continue
            normal_accessor = tree["accessors"][attributes["NORMAL"]]
            if normal_accessor["type"] != "VEC3":
                errors.append(f"{label}: normals must be VEC3")
                continue
            normals = array(tree, binary, attributes["NORMAL"]).astype(np.float64)
            lengths = np.linalg.norm(normals, axis=1)
            invalid = int(np.count_nonzero(~np.isfinite(lengths) | (np.abs(lengths - 1) > 0.001)))
            result["normal_vertices"] += len(normals)
            result["invalid_normal_vertices"] += invalid
            if invalid:
                errors.append(f"{label}: {invalid} non-finite, zero, or non-unit normals (tolerance 0.001)")
    if result["default_scene_triangles"] == 0:
        errors.append("Active scene has no surface triangles")
    result["errors"], result["warnings"] = errors, warnings
    return result


def inventory(root):
    """Keep each URDF reference: repeated fingers/links contribute repeated draws."""
    root = Path(root).resolve()
    usage, models = defaultdict(set), []
    for category in CATEGORIES:
        for path in sorted((root / category).rglob("*.urdf")):
            model = {"path": path.relative_to(root).as_posix(), "visual": [], "collision": []}
            document = ET.parse(path)
            for role in ("visual", "collision"):
                for mesh in document.findall(f".//{role}/geometry/mesh"):
                    target = (path.parent / mesh.get("filename", "")).resolve()
                    if not target.is_relative_to(root):
                        raise ValueError(f"Mesh reference escapes asset root: {target}")
                    relative = target.relative_to(root).as_posix()
                    usage[relative].add(role)
                    model[role].append(relative)
            models.append(model)
    return usage, models


def audit_repository(root):
    root = Path(root).resolve()
    usage, models = inventory(root)
    meshes = []
    by_path = {}
    for category in CATEGORIES:
        for path in sorted((root / category).rglob("*.glb")):
            relative = path.relative_to(root).as_posix()
            try:
                item = {"path": relative, **audit_glb(path, usage[relative])}
            except Exception as exc:
                item = {"path": relative, "errors": [str(exc)], "warnings": []}
            meshes.append(item)
            by_path[relative] = item
    for model in models:
        for role in ("visual", "collision"):
            references = model.pop(role)
            missing = [p for p in references if p not in by_path]
            model[f"{role}_mesh_instances"] = len(references)
            model[f"{role}_triangles"] = sum(by_path.get(p, {}).get("default_scene_triangles", 0) for p in references)
            model[f"{role}_primitives"] = sum(by_path.get(p, {}).get("default_scene_primitives", 0) for p in references)
            # Bytes are unique file transfers, unlike rendering costs above.
            model[f"{role}_unique_bytes"] = sum(by_path.get(p, {}).get("bytes", 0) for p in set(references))
            if missing:
                model.setdefault("errors", []).append(f"Missing GLB references: {missing}")
    summary = {"meshes": len(meshes), "urdfs": len(models),
               "errors": sum(len(x["errors"]) for x in meshes) + sum(len(x.get("errors", [])) for x in models),
               "warnings": sum(len(x["warnings"]) for x in meshes),
               "bytes": sum(x.get("bytes", 0) for x in meshes),
               "stored_triangles": sum(x.get("stored_triangles", 0) for x in meshes),
               "invalid_normal_vertices": sum(x.get("invalid_normal_vertices", 0) for x in meshes),
               "missing_visual_normal_primitives": sum(x.get("missing_visual_normal_primitives", 0) for x in meshes),
               "unreferenced_glbs": sum(not usage[x["path"]] for x in meshes)}
    return {"summary": summary, "models": models, "meshes": meshes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    report = audit_repository(args.root)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report["summary"]))
    return int(report["summary"]["errors"] > 0 or not report["summary"]["meshes"])


if __name__ == "__main__":
    raise SystemExit(main())
