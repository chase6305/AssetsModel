"""Pack a URDF or mesh for Blender rendering without ROS or Blender import plugins."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import collada
import numpy as np
import trimesh


def origin(element):
    if element is None:
        return np.eye(4)
    rotation = [float(x) for x in element.get("rpy", "0 0 0").split()]
    matrix = trimesh.transformations.euler_matrix(*rotation)
    matrix[:3, 3] = [float(x) for x in element.get("xyz", "0 0 0").split()]
    return matrix


def load_parts(path, transform, color):
    if path.suffix.lower() == ".glb":
        scene = trimesh.load_scene(path, process=False)
        for node in scene.graph.nodes_geometry:
            local, name = scene.graph[node]
            geometry = scene.geometry[name]
            if not isinstance(geometry, trimesh.Trimesh):
                continue
            matrix = transform @ local
            vertices = trimesh.transform_points(geometry.vertices, matrix)
            normals = geometry.vertex_normals[geometry.faces].reshape(-1, 3) @ np.linalg.inv(matrix[:3, :3])
            normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-15)
            material = getattr(geometry.visual, "material", None)
            diffuse = color
            if material is not None:
                diffuse = (np.asarray(material.main_color) / 255).tolist()
            yield vertices, geometry.faces, normals, diffuse
    elif path.suffix.lower() == ".dae":
        doc = collada.Collada(str(path))
        for geometry in doc.scene.objects("geometry"):
            for primitive in geometry.primitives():
                if isinstance(primitive, collada.polylist.BoundPolylist):
                    primitive = primitive.triangleset()
                if not isinstance(primitive, collada.triangleset.BoundTriangleSet):
                    continue
                local = np.eye(4)
                local[:3, :3] *= doc.assetInfo.unitmeter or 1.0
                matrix = transform @ local
                vertices = trimesh.transform_points(primitive.vertex, matrix)
                if primitive.normal is None or primitive.normal_index is None:
                    mesh = trimesh.Trimesh(vertices=vertices, faces=primitive.vertex_index, process=False)
                    normals = np.repeat(mesh.face_normals, 3, axis=0)
                else:
                    normals = primitive.normal[primitive.normal_index].reshape(-1, 3)
                    normals = normals @ np.linalg.inv(matrix[:3, :3])
                normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-15)
                material = primitive.material
                diffuse = material.effect.diffuse if material is not None else color
                if not isinstance(diffuse, (tuple, list, np.ndarray)):
                    diffuse = color
                yield vertices, primitive.vertex_index, normals, list(diffuse)
    else:
        mesh = trimesh.load_mesh(path, process=False)
        mesh.apply_transform(transform)
        yield mesh.vertices, mesh.faces, np.repeat(mesh.face_normals, 3, axis=0), color


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    items = []
    if args.asset.suffix.lower() == ".urdf":
        robot = ET.parse(args.asset).getroot()
        links = {x.get("name"): x for x in robot.findall("link")}
        children = {x.find("child").get("link") for x in robot.findall("joint")}
        roots = set(links) - children
        if len(roots) != 1:
            raise ValueError("Preview requires one root link")
        frames = {roots.pop(): np.eye(4)}
        pending = list(robot.findall("joint"))
        while pending:
            ready = [j for j in pending if j.find("parent").get("link") in frames]
            if not ready:
                raise ValueError("Disconnected or cyclic URDF")
            for joint in ready:
                frames[joint.find("child").get("link")] = frames[joint.find("parent").get("link")] @ origin(joint.find("origin"))
                pending.remove(joint)
        materials = {m.get("name"): m for m in robot.findall("material")}
        for name, link in links.items():
            for visual in link.findall("visual"):
                mesh = visual.find("geometry/mesh")
                if mesh is None:
                    continue
                scale = np.eye(4)
                scale[:3, :3] = np.diag([float(x) for x in mesh.get("scale", "1 1 1").split()])
                matrix = frames[name] @ origin(visual.find("origin")) @ scale
                material = visual.find("material")
                color = [0.6, 0.65, 0.7, 1]
                if material is not None:
                    rgba = material.find("color")
                    if rgba is None and material.get("name") in materials:
                        rgba = materials[material.get("name")].find("color")
                    if rgba is not None:
                        color = [float(x) for x in rgba.get("rgba").split()]
                items.extend(load_parts(args.asset.parent / mesh.get("filename"), matrix, color))
    else:
        items.extend(load_parts(args.asset, np.eye(4), [0.48, 0.52, 0.58, 1]))
    if not items:
        raise ValueError("No visual meshes")
    metadata = []
    for index, (vertices, faces, normals, color) in enumerate(items):
        path = args.output / f"part-{index}.npz"
        np.savez(path, vertices=vertices, faces=faces, normals=normals)
        metadata.append({"file": path.name, "color": color})
    (args.output / "scene.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Prepared {len(items)} mesh parts in {args.output}")


if __name__ == "__main__":
    main()
