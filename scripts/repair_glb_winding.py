"""Find isolated reversed GLB faces using both topology and stored normals.

Only faces with three manifold edges traversed in the same direction by their
neighbors, all three normals pointing backwards, no coincident duplicate face,
and three normally oriented neighbors are eligible. Positions, normals,
materials and JSON bytes are preserved. Default is a dry run.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct

import numpy as np

from audit_asset_quality import CATEGORIES, array
from glb_format import COMPONENTS, read_glb, validate_glb


def isolated_flips(tree, binary):
    indices_used = Counter(p["indices"] for m in tree["meshes"] for p in m["primitives"])
    views_used = Counter(a["bufferView"] for a in tree["accessors"])
    candidates = []
    for mesh_index, mesh in enumerate(tree["meshes"]):
        for primitive_index, primitive in enumerate(mesh["primitives"]):
            if primitive.get("mode", 4) != 4 or "NORMAL" not in primitive["attributes"]:
                continue
            attributes = primitive["attributes"]
            accessor = tree["accessors"][primitive["indices"]]
            if indices_used[primitive["indices"]] != 1 or views_used[accessor["bufferView"]] != 1:
                continue  # Shared index storage could modify another primitive.
            view = tree["bufferViews"][accessor["bufferView"]]
            start, end = view.get("byteOffset", 0), view.get("byteOffset", 0) + view["byteLength"]
            if any(i != accessor["bufferView"] and
                   max(start, other.get("byteOffset", 0)) <
                   min(end, other.get("byteOffset", 0) + other["byteLength"])
                   for i, other in enumerate(tree["bufferViews"])):
                continue
            positions = array(tree, binary, attributes["POSITION"]).astype(np.float64)
            normals = array(tree, binary, attributes["NORMAL"]).astype(np.float64)
            if normals.shape != positions.shape or not np.isfinite(normals).all():
                continue
            faces = array(tree, binary, primitive["indices"]).ravel().reshape(-1, 3)
            triangles = positions[faces]
            cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
            lengths = np.linalg.norm(cross, axis=1)
            normal_lengths = np.linalg.norm(normals, axis=1)
            dot = np.einsum("ijk,ik->ij", normals[faces], cross)
            cosine = dot / np.maximum(lengths[:, None] * normal_lengths[faces], 1e-30)
            possible = np.flatnonzero((cosine < -0.001).all(axis=1) & (lengths > 1e-20))
            if not len(possible):
                continue
            # Weld positions for adjacency, preserving render seams in the file.
            _, welded = np.unique(positions, axis=0, return_inverse=True)
            welded_faces = welded[faces]
            edges = np.stack((welded_faces[:, [0, 1]], welded_faces[:, [1, 2]],
                              welded_faces[:, [2, 0]]), axis=1).reshape(-1, 2)
            _, groups, counts = np.unique(np.sort(edges, axis=1), axis=0,
                                           return_inverse=True, return_counts=True)
            signs = np.where(edges[:, 0] < edges[:, 1], 1, -1)
            balance = np.bincount(groups, weights=signs)
            order = np.argsort(groups, kind="stable")
            first = np.cumsum(counts) - counts
            _, triangle_groups, triangle_counts = np.unique(np.sort(welded_faces, axis=1), axis=0,
                                                            return_inverse=True, return_counts=True)
            for face in possible:
                edge_groups = groups[3 * face:3 * face + 3]
                if triangle_counts[triangle_groups[face]] != 1:
                    continue
                if not np.all(counts[edge_groups] == 2) or not np.all(np.abs(balance[edge_groups]) == 2):
                    continue
                neighbors = []
                for group in edge_groups:
                    paired = order[first[group]:first[group] + 2] // 3
                    neighbors.append(int(paired[paired != face][0]))
                if len(set(neighbors)) != 3 or not np.all(cosine[neighbors] > 0.001):
                    continue
                candidates.append({"mesh": mesh_index, "primitive": primitive_index,
                                   "triangle": int(face), "index_accessor": primitive["indices"],
                                   "indices_before": faces[face].astype(int).tolist(),
                                   "neighbor_triangles": neighbors,
                                   "corner_normal_cosines": cosine[face].tolist(),
                                   "neighbor_corner_normal_cosines": cosine[neighbors].tolist(),
                                   "centroid": triangles[face].mean(axis=0).tolist(),
                                   "area": float(lengths[face] / 2)})
    if not candidates:
        return candidates
    # Different material primitives may intentionally carry coincident faces.
    # Exclude those too; otherwise the change can expose the wrong material.
    all_triangles = []
    for mesh in tree["meshes"]:
        for primitive in mesh["primitives"]:
            if primitive.get("mode", 4) == 4:
                positions = array(tree, binary, primitive["attributes"]["POSITION"])
                faces = array(tree, binary, primitive["indices"]).ravel().reshape(-1, 3)
                all_triangles.append(positions[faces])
    all_triangles = np.concatenate(all_triangles)
    order = np.lexsort((all_triangles[:, :, 2], all_triangles[:, :, 1], all_triangles[:, :, 0]), axis=1)
    canonical = np.take_along_axis(all_triangles, order[:, :, None], axis=1)
    safe = []
    for candidate in candidates:
        primitive = tree["meshes"][candidate["mesh"]]["primitives"][candidate["primitive"]]
        positions = array(tree, binary, primitive["attributes"]["POSITION"])
        triangle = positions[candidate["indices_before"]]
        triangle = triangle[np.lexsort((triangle[:, 2], triangle[:, 1], triangle[:, 0]))]
        if int(np.count_nonzero(np.all(canonical == triangle, axis=(1, 2)))) == 1:
            safe.append(candidate)
    return safe


def repair(payload):
    validate_glb(payload)
    tree, binary = read_glb(payload)
    if any(key in tree for key in ("animations", "skins", "extensionsRequired")):
        raise ValueError("Only static uncompressed GLBs are supported")
    if any(set(node) - {"mesh", "name"} for node in tree.get("nodes", [])):
        raise ValueError("Only flat identity-node GLBs are supported")
    candidates = isolated_flips(tree, binary)
    output = bytearray(payload)
    binary_start = 28 + struct.unpack_from("<I", payload, 12)[0]
    for item in candidates:
        accessor = tree["accessors"][item["index_accessor"]]
        view = tree["bufferViews"][accessor["bufferView"]]
        component, size = COMPONENTS[accessor["componentType"]]
        stride = view.get("byteStride", size)
        start = binary_start + view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        # Reorder complete indexed corners; attribute arrays are not touched.
        indices = item["indices_before"]
        for corner, value in ((1, indices[2]), (2, indices[1])):
            struct.pack_into("<" + component, output,
                             start + (item["triangle"] * 3 + corner) * stride, value)
    output = bytes(output)
    validate_glb(output)
    if candidates and isolated_flips(*read_glb(output)):
        raise ValueError("Repair did not resolve every isolated flipped face")
    return output, candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="*")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir")
    if args.apply and args.backup_dir.resolve().is_relative_to(root):
        parser.error("Keep backups outside the asset tree")
    paths = args.paths or [Path(category) for category in CATEGORIES]
    files = set()
    for path in paths:
        path = path if path.is_absolute() else root / path
        files.update(path.rglob("*.glb") if path.is_dir() else [path])
    results = []
    for path in sorted(files):
        relative = path.resolve().relative_to(root)
        row = {"path": relative.as_posix()}
        try:
            payload = path.read_bytes()
            output, evidence = repair(payload)
            row.update(status="eligible" if evidence else "unchanged", faces=len(evidence), evidence=evidence,
                       sha256_before=hashlib.sha256(payload).hexdigest(),
                       sha256_after=hashlib.sha256(output).hexdigest())
            if args.apply and evidence:
                backup = (args.backup_dir / relative).resolve()
                if backup.is_relative_to(root):
                    raise ValueError("Backup resolves inside the asset root")
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists() and backup.read_bytes() != payload:
                    raise ValueError("Existing backup differs; choose a fresh backup directory")
                if not backup.exists():
                    with backup.open("xb") as stream:
                        stream.write(payload)
                staging = path.with_suffix(".glb.tmp")
                staging.write_bytes(output)
                staging.replace(path)
                row["status"] = "repaired"
        except Exception as exc:
            row.update(status="error", error=str(exc))
        results.append(row)
    summary = {"files": len(results), "faces": sum(x.get("faces", 0) for x in results),
               "errors": sum(x["status"] == "error" for x in results), "applied": args.apply}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"summary": summary, "meshes": results}, indent=2, allow_nan=False) + "\n")
    print(json.dumps(summary))
    return int(summary["errors"] > 0 or not results)


if __name__ == "__main__":
    raise SystemExit(main())
