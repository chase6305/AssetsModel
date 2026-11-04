"""Pack exact visual geometry/PBR and an optional URDF joint pose for Blender.

GLB baseColorFactor is linear and is copied without an 8-bit/sRGB round trip.
"""
from __future__ import annotations

import argparse
import json
import math
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

import collada
import numpy as np
import trimesh
from glb_format import accessor_bytes, read_glb

DTYPES = {"b": "i1", "B": "u1", "h": "<i2", "H": "<u2", "I": "<u4", "f": "<f4"}


def origin(element):
    if element is None:
        return np.eye(4)
    rotation = [float(x) for x in element.get("rpy", "0 0 0").split()]
    matrix = trimesh.transformations.euler_matrix(*rotation)
    matrix[:3, 3] = [float(x) for x in element.get("xyz", "0 0 0").split()]
    return matrix


def accessor_array(tree, binary, index):
    raw, component, width = accessor_bytes(tree, binary, index)
    result = np.frombuffer(raw, dtype=DTYPES[component]).reshape(-1, width)
    if tree["accessors"][index].get("normalized"):
        info = np.iinfo(result.dtype)
        result = np.maximum(result.astype(float) / info.max, -1)
    return result


def node_matrix(node):
    if "matrix" in node:
        return np.asarray(node["matrix"], dtype=float).reshape(4, 4).T
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    matrix = trimesh.transformations.quaternion_matrix([w, x, y, z])
    matrix[:3, :3] = matrix[:3, :3] @ np.diag(node.get("scale", [1, 1, 1]))
    matrix[:3, 3] = node.get("translation", [0, 0, 0])
    return matrix


def surface_part(vertices, faces, normals, matrix, material, **extra):
    vertices = trimesh.transform_points(vertices, matrix)
    faces = np.asarray(faces, dtype=np.int64).copy()
    if not np.isfinite(vertices).all():
        raise ValueError("Non-finite preview vertices")
    if np.linalg.det(matrix[:3, :3]) < 0:
        faces = faces[:, [0, 2, 1]]
        if normals is not None:
            normals = np.asarray(normals).reshape(-1, 3, 3)[:, [0, 2, 1]].reshape(-1, 3)
        if "uv" in extra:
            extra["uv"] = np.asarray(extra["uv"]).reshape(-1, 3, 2)[:, [0, 2, 1]].reshape(-1, 2)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    face_normals = np.repeat(mesh.face_normals, 3, axis=0)
    if normals is None:
        normals = face_normals
    else:
        normals = np.asarray(normals).reshape(-1, 3) @ np.linalg.inv(matrix[:3, :3])
        if not np.isfinite(normals).all():
            raise ValueError("Non-finite preview normals")
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = np.divide(normals, lengths, out=face_normals.copy(), where=lengths > 1e-15)
    return {"vertices": vertices, "faces": faces, "normals": normals, "material": material, **extra}


def fallback_material(color):
    return {"color": list(color) if color is not None else [0.6, 0.65, 0.7, 1], "metallic": 0.0, "roughness": 0.4,
            "alpha_mode": "OPAQUE", "double_sided": True}


def gltf_material(source):
    pbr = source.get("pbrMetallicRoughness", {})
    return {"color": pbr.get("baseColorFactor", [1, 1, 1, 1]),
            "metallic": pbr.get("metallicFactor", 1.0),
            "roughness": pbr.get("roughnessFactor", 1.0),
            "alpha_mode": source.get("alphaMode", "OPAQUE"),
            "alpha_cutoff": source.get("alphaCutoff", 0.5),
            "double_sided": source.get("doubleSided", False),
            "material_name": source.get("name", "")}


def load_parts(path, transform, color):
    path = Path(path)
    if path.suffix.lower() == ".glb":
        tree, binary = read_glb(path)
        if tree.get("skins") or tree.get("animations"):
            raise ValueError(f"Animated/skinned GLB preview is unsupported: {path}")
        nodes = tree.get("nodes", [])
        scene = tree.get("scenes", [])[tree.get("scene", 0)]
        stack = [(index, transform, set()) for index in reversed(scene.get("nodes", []))]
        while stack:
            index, parent, ancestors = stack.pop()
            if index in ancestors:
                raise ValueError("Cyclic GLB node hierarchy")
            node = nodes[index]
            matrix = parent @ node_matrix(node)
            stack.extend((child, matrix, ancestors | {index}) for child in reversed(node.get("children", [])))
            if "mesh" not in node:
                continue
            for primitive in tree["meshes"][node["mesh"]]["primitives"]:
                if primitive.get("mode", 4) != 4:
                    continue
                attrs = primitive["attributes"]
                vertices = accessor_array(tree, binary, attrs["POSITION"])
                faces = (accessor_array(tree, binary, primitive["indices"]).reshape(-1, 3)
                         if "indices" in primitive else np.arange(len(vertices)).reshape(-1, 3))
                normals = accessor_array(tree, binary, attrs["NORMAL"])[faces].reshape(-1, 3) if "NORMAL" in attrs else None
                if "COLOR_0" in attrs:
                    raise ValueError(f"Vertex-colored preview is unsupported: {path}")
                # URDF color may supply an appearance for a material-less resource.
                # A standalone GLB must use glTF's default white metal material.
                material = fallback_material(color) if color is not None else gltf_material({})
                if "material" in primitive:
                    source = tree["materials"][primitive["material"]]
                    pbr = source.get("pbrMetallicRoughness", {})
                    # Fail visibly rather than produce an inaccurate untextured review image.
                    if any("Texture" in key for key in pbr) or any("Texture" in key for key in source):
                        raise ValueError(f"Textured GLB preview requires texture export support: {path}")
                    material = gltf_material(source)
                yield surface_part(vertices, faces, normals, matrix, material)
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
                normals = (primitive.normal[primitive.normal_index].reshape(-1, 3)
                           if primitive.normal is not None and primitive.normal_index is not None else None)
                diffuse = primitive.material.effect.diffuse if primitive.material is not None else color
                if not isinstance(diffuse, (tuple, list, np.ndarray)):
                    diffuse = color
                yield surface_part(primitive.vertex, primitive.vertex_index, normals, transform @ local,
                                   fallback_material(diffuse))
    else:
        mesh = trimesh.load_mesh(path, process=False)
        yield surface_part(mesh.vertices, mesh.faces, None, transform, fallback_material(color))


def primitive_part(geometry, transform, color):
    """Tessellate URDF primitives for preview, retaining flat caps and smooth sides."""
    shapes = list(geometry)
    if len(shapes) != 1:
        raise ValueError("Visual geometry must contain exactly one shape")
    shape = shapes[0]

    def positive(attribute, count=1):
        values = np.asarray([float(x) for x in shape.get(attribute, "").split()])
        if len(values) != count or not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError(f"Invalid positive {shape.tag} {attribute}")
        return values if count != 1 else values[0]

    if shape.tag == "box":
        mesh = trimesh.creation.box(extents=positive("size", 3))
        normals = None  # Exact flat face normals, including the triangle diagonals.
    elif shape.tag == "cylinder":
        mesh = trimesh.creation.cylinder(radius=positive("radius"), height=positive("length"), sections=64)
        normals = mesh.vertices[mesh.faces].copy()
        normals[:, :, 2] = 0
        cap = np.abs(mesh.face_normals[:, 2]) > 0.5
        normals[cap] = mesh.face_normals[cap, None, :]
        lengths = np.linalg.norm(normals, axis=2, keepdims=True)
        normals = (normals / np.maximum(lengths, 1e-15)).reshape(-1, 3)
    elif shape.tag == "sphere":
        radius = positive("radius")
        mesh = trimesh.creation.icosphere(subdivisions=3, radius=radius)
        normals = (mesh.vertices / radius)[mesh.faces].reshape(-1, 3)
    else:
        raise ValueError(f"Unsupported visual geometry: {shape.tag}")
    return surface_part(mesh.vertices, mesh.faces, normals, transform, fallback_material(color))


def joint_frames(robot, values=None, allow_outside_limits=False):
    """URDF parent * joint origin * local axis motion, with chained mimic values."""
    values = {} if values is None else values
    if not isinstance(values, dict):
        raise ValueError("Joint pose must be an object mapping joint names to radians/metres")
    joints = {j.get("name"): j for j in robot.findall("joint")}
    unknown = set(values) - joints.keys()
    if unknown:
        raise ValueError(f"Unknown joints: {sorted(unknown)}")
    resolved, active = {}, set()

    def resolve(name):
        if name in resolved:
            return resolved[name]
        if name in active:
            raise ValueError("Cyclic mimic joints")
        if name not in joints:
            raise ValueError(f"Unknown mimic source: {name}")
        active.add(name)
        joint = joints[name]
        mimic = joint.find("mimic")
        kind = joint.get("type")
        if name in values and (mimic is not None or kind == "fixed"):
            raise ValueError(f"Cannot set a fixed or mimic joint: {name}")
        if mimic is not None:
            value = resolve(mimic.get("joint")) * float(mimic.get("multiplier", 1)) + float(mimic.get("offset", 0))
        else:
            value = float(values.get(name, 0))
        if not math.isfinite(value):
            raise ValueError(f"Non-finite joint value: {name}")
        limit = joint.find("limit")
        if kind in ("revolute", "prismatic") and limit is not None:
            lower, upper = float(limit.get("lower", "-inf")), float(limit.get("upper", "inf"))
            if value < lower - 1e-10 or value > upper + 1e-10:
                message = f"Joint {name}={value} outside [{lower}, {upper}]"
                if not allow_outside_limits:
                    raise ValueError(message)
                warnings.warn(message, stacklevel=2)
        active.remove(name)
        resolved[name] = value
        return value

    for name in joints:
        resolve(name)
    links = {x.get("name") for x in robot.findall("link")}
    children = [j.find("child").get("link") for j in joints.values()]
    roots = links - set(children)
    if len(roots) != 1 or len(children) != len(set(children)):
        raise ValueError("Preview requires one root and one parent per child link")
    frames = {roots.pop(): np.eye(4)}
    pending = list(joints.values())
    while pending:
        ready = [j for j in pending if j.find("parent").get("link") in frames]
        if not ready:
            raise ValueError("Disconnected or cyclic URDF")
        for joint in ready:
            motion, kind = np.eye(4), joint.get("type")
            value = resolved[joint.get("name")]
            if kind in ("revolute", "continuous", "prismatic"):
                axis_element = joint.find("axis")
                axis = np.asarray([float(x) for x in (axis_element.get("xyz", "1 0 0") if axis_element is not None else "1 0 0").split()])
                length = np.linalg.norm(axis)
                if not np.isfinite(axis).all() or length <= 1e-15:
                    raise ValueError(f"Invalid joint axis: {joint.get('name')}")
                axis /= length
                if kind == "prismatic":
                    motion[:3, 3] = axis * value
                else:
                    motion = trimesh.transformations.rotation_matrix(value, axis)
            elif kind != "fixed":
                raise ValueError(f"Unsupported scalar preview joint type: {kind}")
            frames[joint.find("child").get("link")] = frames[joint.find("parent").get("link")] @ origin(joint.find("origin")) @ motion
            pending.remove(joint)
    if set(frames) != links:
        raise ValueError("Disconnected URDF links")
    return frames, resolved


def prepare(asset, output, joints=None, allow_outside_limits=False):
    asset, output = Path(asset), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    items, resolved = [], {}
    if asset.suffix.lower() == ".urdf":
        robot = ET.parse(asset).getroot()
        frames, resolved = joint_frames(robot, joints, allow_outside_limits=allow_outside_limits)
        materials = {m.get("name"): m for m in robot.findall("material")}
        for link in robot.findall("link"):
            for visual in link.findall("visual"):
                mesh = visual.find("geometry/mesh")
                scale = np.eye(4)
                if mesh is not None:
                    scale[:3, :3] = np.diag([float(x) for x in mesh.get("scale", "1 1 1").split()])
                matrix = frames[link.get("name")] @ origin(visual.find("origin")) @ scale
                material = visual.find("material")
                color = None
                if material is not None:
                    rgba = material.find("color")
                    if rgba is None and material.get("name") in materials:
                        rgba = materials[material.get("name")].find("color")
                    if rgba is not None:
                        color = [float(x) for x in rgba.get("rgba").split()]
                if mesh is not None:
                    items.extend(load_parts(asset.parent / mesh.get("filename"), matrix, color))
                else:
                    geometry = visual.find("geometry")
                    if geometry is None:
                        raise ValueError("Visual has no geometry")
                    items.append(primitive_part(geometry, matrix, color))
    else:
        if joints:
            raise ValueError("Joint poses require a URDF")
        items.extend(load_parts(asset, np.eye(4), None))
    if not items:
        raise ValueError("No visual meshes")
    metadata = []
    for index, part in enumerate(items):
        path = output / f"part-{index}.npz"
        arrays = {key: value for key, value in part.items() if key != "material"}
        np.savez(path, **arrays)
        metadata.append({"file": path.name, **part["material"]})
    (output / "scene.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (output / "pose.json").write_text(json.dumps({"asset": str(asset), "joints": resolved,
                                                   "allow_outside_limits": allow_outside_limits}, indent=2) + "\n")
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--joints", type=json.loads, help='JSON object of joint positions in radians/metres')
    parser.add_argument("--allow-outside-limits", action="store_true", help="Allow and warn about historical zero-pose limit violations")
    args = parser.parse_args()
    try:
        parts = prepare(args.asset, args.output, joints=args.joints, allow_outside_limits=args.allow_outside_limits)
    except (ValueError, np.linalg.LinAlgError) as exc:
        parser.error(str(exc))
    print(f"Prepared {len(parts)} mesh parts in {args.output}")


if __name__ == "__main__":
    main()
