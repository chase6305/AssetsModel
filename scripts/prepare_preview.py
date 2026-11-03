"""Pack a URDF or mesh for Blender rendering without ROS or Blender import plugins."""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import collada
import numpy as np
import trimesh

from glb_format import accessor_bytes, read_glb


def origin(element):
    if element is None:
        return np.eye(4)
    rotation = [float(x) for x in element.get("rpy", "0 0 0").split()]
    matrix = trimesh.transformations.euler_matrix(*rotation)
    matrix[:3, 3] = [float(x) for x in element.get("xyz", "0 0 0").split()]
    return matrix


def accessor_array(tree, binary, index):
    """Read a packed or interleaved accessor without quantizing material data."""
    raw, component, width = accessor_bytes(tree, binary, index)
    values = np.frombuffer(raw, dtype=np.dtype("<" + component)).reshape(-1, width)
    accessor = tree["accessors"][index]
    if accessor.get("normalized"):
        info = np.iinfo(values.dtype)
        values = np.maximum(values.astype(np.float64) / info.max, -1)
    return values


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
    if path.suffix.lower() == ".glb":
        yield from glb_parts(path, transform, color)
    elif path.suffix.lower() == ".dae":
        from convert_glb import dae_material

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
                    raise ValueError("Textured DAE preview is unsupported; convert it to GLB first")
                properties = (glb_material({"materials": [dae_material(material)]}, b"", 0, color)
                              if material is not None else glb_material({}, b"", None, list(diffuse)))
                yield {"vertices": vertices, "faces": primitive.vertex_index, "normals": normals, "material": properties}
    else:
        mesh = trimesh.load_mesh(path, process=False)
        vertices, faces, normals = transform_part(mesh.vertices, mesh.faces, None, transform)
        yield {"vertices": vertices, "faces": faces, "normals": normals,
               "material": glb_material({}, b"", None, color)}


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
        metadata.append({"file": path.name, "color": material["base_color"], "material": material})
    (output / "scene.json").write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n")
    return len(metadata)


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
        # Standalone GLB follows glTF's implicit white metallic material. A URDF
        # supplies its own fallback above; unpainted STL keeps a neutral display.
        color = None if args.asset.suffix.lower() == ".glb" else [0.48, 0.52, 0.58, 1]
        items.extend(load_parts(args.asset, np.eye(4), color))
    if not items:
        raise ValueError("No visual meshes")
    count = write_pack(items, args.output)
    print(f"Prepared {count} mesh parts in {args.output}")


if __name__ == "__main__":
    main()
