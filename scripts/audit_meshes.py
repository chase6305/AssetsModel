"""Load all mesh geometry and report invalid indices, vertices and degenerate faces."""

import argparse
import json
from pathlib import Path

import collada
import numpy as np
import trimesh


def audit(path):
    meshes = []
    if path.suffix.lower() == ".dae":
        doc = collada.Collada(str(path))
        for geometry in doc.geometries:
            for primitive in geometry.primitives:
                if isinstance(primitive, collada.polylist.Polylist):
                    primitive = primitive.triangleset()
                if isinstance(primitive, collada.triangleset.TriangleSet):
                    meshes.append((primitive.vertex, primitive.vertex_index))
    elif path.suffix.lower() == ".glb":
        scene = trimesh.load_scene(path, process=False)
        for node in scene.graph.nodes_geometry:
            matrix, name = scene.graph[node]
            geometry = scene.geometry[name]
            if isinstance(geometry, trimesh.Trimesh):
                meshes.append((trimesh.transform_points(geometry.vertices, matrix), geometry.faces))
                normals = geometry.vertex_normals
                if not np.isfinite(normals).all():
                    raise ValueError("Non-finite GLB normals")
    else:
        mesh = trimesh.load_mesh(path, process=False)
        meshes.append((mesh.vertices, mesh.faces))
    faces_total = degenerate = 0
    bounds = []
    for vertices, faces in meshes:
        if not np.isfinite(vertices).all():
            raise ValueError("Non-finite vertices")
        if not len(faces):
            continue
        if faces.min() < 0 or faces.max() >= len(vertices):
            raise ValueError("Invalid vertex index")
        triangles = vertices[faces].astype(np.float64)
        cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
        degenerate += int(np.count_nonzero(np.linalg.norm(cross, axis=1) <= 1e-20))
        faces_total += len(faces)
        bounds.extend([vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()])
    if not faces_total:
        raise ValueError("No triangles")
    return {"faces": faces_total, "degenerate_faces": degenerate,
            "local_bounds": [np.min(bounds, axis=0).tolist(), np.max(bounds, axis=0).tolist()]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    results, errors = [], 0
    for category in ("CameraModel", "RobotModel", "ToolModel"):
        for path in sorted((args.root / category).rglob("*")):
            if path.suffix.lower() not in (".stl", ".dae", ".glb"):
                continue
            item = {"path": path.relative_to(args.root).as_posix()}
            try:
                item.update(audit(path))
            except Exception as exc:
                errors += 1
                item["error"] = str(exc)
                print(f"ERROR {item['path']}: {exc}", flush=True)
            results.append(item)
    summary = {"meshes": len(results), "errors": errors,
               "faces": sum(x.get("faces", 0) for x in results),
               "degenerate_faces": sum(x.get("degenerate_faces", 0) for x in results)}
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps({"summary": summary, "meshes": results}, indent=2) + "\n")
    print(json.dumps(summary))
    return int(errors > 0 or not results)


if __name__ == "__main__":
    raise SystemExit(main())
