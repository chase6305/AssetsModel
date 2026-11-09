"""Pack exact visual geometry/PBR and an optional URDF joint pose for Blender.

GLB baseColorFactor is linear and is copied without an 8-bit/sRGB round trip.
"""
from __future__ import annotations

import argparse
import hashlib
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


def transform_part(vertices, faces, normals, matrix):
    """Bake transforms, preserving front faces under mirrored mesh scales."""
    linear = matrix[:3, :3]
    if abs(np.linalg.det(linear)) < 1e-15:
        raise ValueError("Preview cannot apply a singular mesh transform")
    vertices = trimesh.transform_points(vertices, matrix)
    if normals is None:
        triangles = vertices[faces]
        face_normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
        normals = np.repeat(face_normals, 3, axis=0)
        if np.linalg.det(linear) < 0:
            normals *= -1
    else:
        normals = normals[faces].reshape(-1, 3) @ np.linalg.inv(linear)
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-15)
    if np.linalg.det(linear) < 0:
        faces = faces[:, ::-1]
        normals = normals.reshape(-1, 3, 3)[:, ::-1].reshape(-1, 3)
    return vertices, faces, normals


def embedded_image(tree, binary, info):
    if info.get("texCoord", 0) != 0 or info.get("extensions"):
        raise ValueError("Preview supports TEXCOORD_0 without texture transforms")
    texture = tree["textures"][info["index"]]
    image = tree["images"][texture["source"]]
    if "bufferView" not in image:
        raise ValueError("Preview requires an embedded GLB image")
    view = tree["bufferViews"][image["bufferView"]]
    start = view.get("byteOffset", 0)
    mime = image.get("mimeType")
    if mime not in ("image/png", "image/jpeg"):
        raise ValueError(f"Unsupported preview image type: {mime}")
    sampler = tree.get("samplers", [])[texture["sampler"]] if "sampler" in texture else {}
    wraps = (sampler.get("wrapS", 10497), sampler.get("wrapT", 10497))
    if wraps[0] != wraps[1] or wraps[0] not in (10497, 33071, 33648):
        raise ValueError("Preview requires equal S/T texture wrap modes")
    return {"data": binary[start:start + view["byteLength"]],
            "suffix": ".png" if mime == "image/png" else ".jpg",
            "extension": {10497: "REPEAT", 33071: "EXTEND", 33648: "MIRROR"}[wraps[0]],
            "interpolation": "Closest" if sampler.get("magFilter") == 9728 else "Linear"}


def glb_material(tree, binary, index, color):
    # A URDF color is a fallback for unpainted source STL meshes. Embedded GLB
    # material groups take precedence so imported CAD colors are not flattened.
    if index is None:
        standalone = color is None
        color = [1, 1, 1, 1] if standalone else color
        return {"base_color": color, "metallic": 1.0 if standalone else 0.0, "roughness": 1.0,
                "emissive": [0, 0, 0], "alpha_mode": "BLEND" if color[3] < 1 else "OPAQUE", "double_sided": False}
    material = tree["materials"][index]
    pbr = material.get("pbrMetallicRoughness", {})
    unsupported = [key for key in material if key.endswith("Texture")]
    unsupported += [key for key in pbr if key.endswith("Texture") and key != "baseColorTexture"]
    if material.get("extensions") or unsupported:
        raise ValueError(f"Unsupported preview material features: {unsupported or material['extensions']}")
    result = {"base_color": pbr.get("baseColorFactor", [1, 1, 1, 1]),
              "metallic": pbr.get("metallicFactor", 1.0),
              "roughness": pbr.get("roughnessFactor", 1.0),
              "emissive": material.get("emissiveFactor", [0, 0, 0]),
              "alpha_mode": material.get("alphaMode", "OPAQUE"),
              "alpha_cutoff": material.get("alphaCutoff", 0.5),
              "double_sided": material.get("doubleSided", False)}
    if "baseColorTexture" in pbr:
        result["base_color_texture"] = embedded_image(tree, binary, pbr["baseColorTexture"])
    return result


def glb_parts(path, transform, color):
    tree, binary = read_glb(path)
    if tree.get("skins") or tree.get("animations"):
        raise ValueError(f"Animated/skinned GLB preview is unsupported: {path}")
    if tree.get("extensionsRequired"):
        raise ValueError("Preview does not support required GLB extensions")

    def walk(index, parent, ancestors):
        if index in ancestors:
            raise ValueError("Cyclic GLB node hierarchy")
        node = tree["nodes"][index]
        if "skin" in node or node.get("weights"):
            raise ValueError("Preview does not support skinned or morphed geometry")
        if "matrix" in node:
            local = np.asarray(node["matrix"], dtype=float).reshape(4, 4).T
        else:
            q = node.get("rotation", [0, 0, 0, 1])
            local = trimesh.transformations.quaternion_matrix([q[3], *q[:3]])
            local[:3, :3] = local[:3, :3] @ np.diag(node.get("scale", [1, 1, 1]))
            local[:3, 3] = node.get("translation", [0, 0, 0])
        matrix = parent @ local
        if "mesh" in node:
            for primitive in tree["meshes"][node["mesh"]]["primitives"]:
                if primitive.get("mode", 4) != 4:
                    continue
                if primitive.get("targets") or primitive.get("extensions"):
                    raise ValueError("Preview does not support morphed or extended primitives")
                attributes = primitive["attributes"]
                if set(attributes) - {"POSITION", "NORMAL", "TEXCOORD_0"}:
                    raise ValueError(f"Unsupported preview vertex attributes: {set(attributes)}")
                vertices = accessor_array(tree, binary, attributes["POSITION"])
                indices = (accessor_array(tree, binary, primitive["indices"]).ravel()
                           if "indices" in primitive else np.arange(len(vertices)))
                faces = indices.reshape(-1, 3)
                normals = accessor_array(tree, binary, attributes["NORMAL"]) if "NORMAL" in attributes else None
                vertices, faces, normals = transform_part(vertices, faces, normals, matrix)
                material = glb_material(tree, binary, primitive.get("material"), color)
                part = {"vertices": vertices, "faces": faces, "normals": normals, "material": material}
                if "base_color_texture" in material:
                    if "TEXCOORD_0" not in attributes:
                        raise ValueError("Textured primitive has no TEXCOORD_0")
                    uv = accessor_array(tree, binary, attributes["TEXCOORD_0"]).astype(float)
                    uv[:, 1] = 1 - uv[:, 1]  # glTF top-left origin -> Blender bottom-left.
                    part["uv"] = uv[faces].reshape(-1, 2)
                yield part
        for child in node.get("children", []):
            yield from walk(child, matrix, ancestors | {index})

    scene = tree["scenes"][tree.get("scene", 0)]
    for index in scene.get("nodes", []):
        yield from walk(index, transform, set())


def load_parts(path, transform, color):
    path = Path(path)
    if path.suffix.lower() == ".glb":
        for part in glb_parts(path, transform, color):
            part["material"]["color"] = part["material"]["base_color"]
            yield part
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


def write_pack(items, output):
    output.mkdir(parents=True, exist_ok=True)
    metadata = []
    for index, item in enumerate(items):
        path = output / f"part-{index}.npz"
        arrays = {key: item[key] for key in ("vertices", "faces", "normals", "uv") if key in item}
        np.savez(path, **arrays)
        material = dict(item["material"])
        if "base_color_texture" in material:
            image = dict(material["base_color_texture"])
            data = image.pop("data")
            image["file"] = "texture-" + hashlib.sha256(data).hexdigest()[:16] + image.pop("suffix")
            (output / image["file"]).write_bytes(data)
            material["base_color_texture"] = image
        metadata.append({"file": path.name, **material, "color": material.get("base_color", material.get("color")), "material": material})
    (output / "scene.json").write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n")
    return len(metadata)


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
    write_pack(items, output)
    metadata = json.loads((output / "scene.json").read_text())
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
