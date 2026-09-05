"""Convert incoming DAE/STL files, or an archived source tree, to GLB."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
from pathlib import Path

import collada
import numpy as np
import trimesh
from lxml import etree as ET
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from glb_format import read_glb, accessor_bytes, validate_glb

CATEGORIES = ("CameraModel", "RobotModel", "ToolModel")


class GLBWriter:
    def __init__(self, source):
        self.tree = {"asset": {"version": "2.0", "generator": "AssetsModel",
                              "extras": {"source": source, "coordinateFrame": "URDF mesh local", "units": "meters"}},
                     "scene": 0, "scenes": [{"name": "URDF surfaces", "nodes": []}], "nodes": [], "meshes": [],
                     "accessors": [], "bufferViews": [], "materials": []}
        self.binary = bytearray()
        self.materials = {}

    def accessor(self, values, component, kind, target):
        values = np.ascontiguousarray(values)
        self.binary.extend(b"\0" * (-len(self.binary) % 4))
        view = len(self.tree["bufferViews"])
        self.tree["bufferViews"].append({"buffer": 0, "byteOffset": len(self.binary),
                                         "byteLength": values.nbytes, "target": target})
        self.binary.extend(values.tobytes())
        accessor = {"bufferView": view, "componentType": component,
                    "count": len(values), "type": kind}
        if kind == "VEC3" and target == 34962:
            accessor.update(min=values.min(axis=0).tolist(), max=values.max(axis=0).tolist())
        index = len(self.tree["accessors"])
        self.tree["accessors"].append(accessor)
        return index

    def add(self, name, vertices, indices, normals=None, material=None, mode=4):
        vertices = np.asarray(vertices, dtype="<f4")
        if not len(indices):
            return None
        if not np.isfinite(vertices).all():
            raise ValueError("Non-finite positions")
        attributes = {"POSITION": self.accessor(vertices, 5126, "VEC3", 34962)}
        if normals is not None:
            normals = np.asarray(normals, dtype="<f4")
            if not np.isfinite(normals).all():
                raise ValueError("Non-finite normals")
            attributes["NORMAL"] = self.accessor(normals, 5126, "VEC3", 34962)
        component, dtype = (5123, "<u2") if int(np.max(indices)) < 65535 else (5125, "<u4")
        primitive = {"attributes": attributes, "indices": self.accessor(np.asarray(indices, dtype=dtype).ravel(), component, "SCALAR", 34963), "mode": mode}
        if material is not None:
            key = json.dumps(material, sort_keys=True, allow_nan=False)
            if key not in self.materials:
                self.materials[key] = len(self.tree["materials"])
                self.tree["materials"].append(material)
            primitive["material"] = self.materials[key]
        index = len(self.tree["meshes"])
        self.tree["meshes"].append({"name": name, "primitives": [primitive]})
        if mode == 4:
            self.tree["scenes"][0]["nodes"].append(len(self.tree["nodes"]))
        self.tree["nodes"].append({"name": name, "mesh": index})
        return primitive

    def export(self):
        if len(self.tree["scenes"][0]["nodes"]) < len(self.tree["nodes"]):
            self.tree["scenes"].append({"name": "Surfaces and CAD guides", "nodes": list(range(len(self.tree["nodes"])))})
        self.tree["buffers"] = [{"byteLength": len(self.binary)}]
        # Optional glTF arrays must be omitted when empty (schema minItems=1).
        tree = {key: value for key, value in self.tree.items() if value != []}
        raw = json.dumps(tree, separators=(",", ":"), allow_nan=False).encode()
        raw += b" " * (-len(raw) % 4)
        binary = bytes(self.binary) + b"\0" * (-len(self.binary) % 4)
        size = 12 + 8 + len(raw) + 8 + len(binary)
        return (struct.pack("<III", 0x46546C67, 2, size) + struct.pack("<II", len(raw), 0x4E4F534A)
                + raw + struct.pack("<II", len(binary), 0x004E4942) + binary)


def compact(vertices, indices, normals=None):
    """Share vertices only when their complete attribute tuples match."""
    corners = np.asarray(vertices)[indices].reshape(-1, 3).astype(np.float32)
    if normals is None:
        values, inverse = np.unique(corners, axis=0, return_inverse=True)
        return values, inverse.reshape(indices.shape), None
    normals = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    if np.any(lengths <= 1e-15):
        raise ValueError("Zero corner normal")
    normals = (normals / lengths).astype(np.float32)
    values, inverse = np.unique(np.column_stack((corners, normals)), axis=0, return_inverse=True)
    return values[:, :3], inverse.reshape(indices.shape), values[:, 3:]


def dae_material(material):
    if material is None:
        return None
    effect = material.effect
    if not isinstance(effect.diffuse, (tuple, list, np.ndarray)):
        raise ValueError("Textured DAE needs an image-aware conversion path")
    color = np.asarray(effect.diffuse, dtype=float)
    alpha = float(color[3]) if len(color) == 4 else 1.0
    # A_ONE is the convention used by the repository's source DAE materials.
    if effect.transparent is not None and effect.opaque_mode != "A_ONE":
        raise ValueError("Unsupported DAE transparency convention")
    if effect.transparent is not None:
        alpha *= float(effect.transparent[3])
    if effect.transparency is not None:
        alpha *= float(effect.transparency)
    roughness = 1.0 if effect.shininess is None else float(np.clip(math.sqrt(2 / (float(effect.shininess) + 2)), 0.04, 1))
    result = {"name": material.id,
              "pbrMetallicRoughness": {"baseColorFactor": [*np.clip(color[:3], 0, 1).tolist(), float(np.clip(alpha, 0, 1))],
                                       "metallicFactor": 0.0, "roughnessFactor": roughness},
              "doubleSided": bool(effect.double_sided)}
    if effect.emission is not None:
        result["emissiveFactor"] = np.clip(effect.emission[:3], 0, 1).tolist()
    if alpha < 1:
        result["alphaMode"] = "BLEND"
    return result


def dae_parts(path, collision):
    doc = collada.Collada(str(path))
    if doc.scene is None:
        raise ValueError("DAE has no default scene")
    unit = float(doc.assetInfo.unitmeter or 1)
    def walk(node, matrix, prefix):
        if isinstance(node, collada.scene.Node):
            for child in node.children:
                yield from walk(child, matrix @ node.matrix, prefix + "/" + (node.id or "node"))
        elif isinstance(node, collada.scene.NodeNode):
            yield from walk(node.node, matrix, prefix)
        elif isinstance(node, collada.scene.GeometryNode):
            materials = {m.symbol: m.target for m in node.materials}
            linear = matrix[:3, :3] * unit
            if abs(np.linalg.det(linear)) < 1e-15:
                raise ValueError("Singular DAE transform")
            for index, primitive in enumerate(node.geometry.primitives):
                if isinstance(primitive, collada.polylist.Polylist):
                    primitive = primitive.triangleset()
                if not isinstance(primitive, (collada.triangleset.TriangleSet, collada.lineset.LineSet)):
                    raise ValueError("Unsupported DAE primitive")
                if getattr(primitive, "texcoordset", ()):
                    raise ValueError("Textured DAE needs an attribute-aware conversion path")
                if "COLOR" in primitive.sources:
                    color_source = primitive.sources["COLOR"][0]
                    colors = color_source[4].data[primitive.index[:, :, color_source[0]]]
                    if not np.all(colors == 1):
                        raise ValueError("Non-white vertex colors need an attribute-aware conversion path")
                vertices = np.asarray(primitive.vertex, dtype=np.float64) @ linear.T + matrix[:3, 3] * unit
                indices = primitive.vertex_index.copy()
                mode = 1 if isinstance(primitive, collada.lineset.LineSet) else 4
                normals = None
                if mode == 4:
                    if np.linalg.det(linear) < 0:
                        indices = indices[:, ::-1]
                    if not collision:
                        if primitive.normal is not None:
                            normal_indices = primitive.normal_index[:, ::-1] if np.linalg.det(linear) < 0 else primitive.normal_index
                            normals = primitive.normal[normal_indices].reshape(-1, 3) @ np.linalg.inv(linear)
                        bad = np.ones(indices.size, dtype=bool) if normals is None else np.linalg.norm(normals, axis=1) < 1e-15
                        if np.any(bad):
                            triangles = vertices[indices]
                            geometric = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
                            lengths = np.linalg.norm(geometric, axis=1, keepdims=True)
                            if np.any(lengths <= 1e-20):
                                raise ValueError("Clean degenerate DAE geometry before converting")
                            fallback = np.repeat(geometric / lengths, 3, axis=0)
                            if normals is None:
                                normals = fallback
                            else:
                                normals[bad] = fallback[bad]
                material = None if collision else dae_material(materials.get(primitive.material))
                yield prefix + "/" + node.geometry.id + f"/{index}", vertices, indices, normals, material, mode
    for node in doc.scene.nodes:
        yield from walk(node, np.eye(4), "")


def mesh_parts(path, collision):
    if path.suffix.lower() == ".dae":
        yield from dae_parts(path, collision)
        return
    mesh = trimesh.load_mesh(path, process=False)
    if collision:
        yield path.stem, mesh.vertices, mesh.faces, None, None, 4
    else:
        normals = smooth_stl_normals(mesh.vertices, mesh.faces)
        yield path.stem, mesh.vertices, mesh.faces, normals, None, 4


def smooth_stl_normals(vertices, faces, angle=30):
    """Smooth corner normals across manifold edges below the crease angle.

    Connectivity is local to each vertex fan. Vertex coordinates and triangle
    order are unchanged, including at nonmanifold boundaries.
    """
    welded, inverse = np.unique(vertices, axis=0, return_inverse=True)
    indexed = inverse[faces]
    mesh = trimesh.Trimesh(welded, indexed, process=False)
    triangles = welded[indexed]
    vectors = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(lengths <= 1e-20):
        raise ValueError("Clean degenerate STL triangles before GLB conversion")
    face_normals = vectors / lengths
    pairs = mesh.face_adjacency
    shared = mesh.face_adjacency_edges
    mask = np.einsum("ij,ij->i", face_normals[pairs[:, 0]], face_normals[pairs[:, 1]]) >= math.cos(math.radians(angle))
    pairs, shared = pairs[mask], shared[mask]
    left = np.argmax(indexed[pairs[:, 0], None, :] == shared[:, :, None], axis=2)
    right = np.argmax(indexed[pairs[:, 1], None, :] == shared[:, :, None], axis=2)
    rows = (pairs[:, 0, None] * 3 + left).ravel()
    columns = (pairs[:, 1, None] * 3 + right).ravel()
    graph = coo_matrix((np.ones(len(rows), dtype=bool), (rows, columns)), shape=(faces.size, faces.size))
    count, labels = connected_components(graph, directed=False)
    summed = np.zeros((count, 3))
    np.add.at(summed, labels, np.repeat(vectors, 3, axis=0))
    norm = np.linalg.norm(summed, axis=1, keepdims=True)
    if np.any(norm <= 1e-20):
        raise ValueError("STL has cancelling normals in a smoothing fan")
    return (summed / norm)[labels]


def convert(path, collision):
    writer = GLBWriter(path.name)
    expected = []
    vertices_before = vertices_after = 0
    for name, vertices, indices, normals, material, mode in mesh_parts(path, collision):
        if not len(indices):
            continue
        packed_vertices, packed_indices, packed_normals = compact(vertices, indices, normals)
        vertices_before += indices.size
        vertices_after += len(packed_vertices)
        primitive = writer.add(name, packed_vertices, packed_indices, packed_normals, material, mode)
        expected.append((primitive, vertices[indices].reshape(-1, 3), packed_normals))
    payload = writer.export()
    stats = validate_glb(payload)
    tree, binary = read_glb(payload)
    max_error = 0.0
    # Compare every written corner, rather than sampling only the surface.
    for primitive, corners, normals in expected:
        values, _, _ = accessor_bytes(tree, binary, primitive["attributes"]["POSITION"])
        vertices = np.frombuffer(values, dtype="<f4").reshape(-1, 3)
        values, component, _ = accessor_bytes(tree, binary, primitive["indices"])
        indices = np.frombuffer(values, dtype={"H": "<u2", "I": "<u4"}[component])
        error = float(np.max(np.linalg.norm(vertices[indices] - corners, axis=1)))
        tolerance = max(1e-7, float(np.max(np.abs(corners))) * 2e-7)
        if error > tolerance:
            raise ValueError(f"GLB round-trip moved a vertex: {error} > {tolerance}")
        max_error = max(max_error, error)
        if normals is not None:
            values, _, _ = accessor_bytes(tree, binary, primitive["attributes"]["NORMAL"])
            np.testing.assert_array_equal(np.frombuffer(values, dtype="<f4").reshape(-1, 3), normals)
    return payload, {**stats, "vertex_corners_before": vertices_before, "vertices_after": vertices_after,
                     "max_corner_error_m": max_error, "collision": collision,
                     "normal_policy": "omitted" if collision else "smooth_30_degrees" if path.suffix.lower() == ".stl" else "source_split_normals"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Archived DAE/STL source tree (not the current GLB library)")
    parser.add_argument("--files", nargs="+", type=Path, help="Incoming DAE/STL files to convert into the output directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--collision", action="store_true", help="Omit rendering attributes when using --files")
    parser.add_argument("--overwrite", action="store_true", help="Allow --files to replace an existing, different GLB")
    parser.add_argument("--models", nargs="*", type=Path, help="Optional model directories relative to root")
    parser.add_argument("--referenced-only", action="store_true", help="Export only meshes referenced by the selected URDFs; requires a fresh output directory")
    args = parser.parse_args()
    if args.files:
        if args.root or args.models or args.referenced_only:
            parser.error("--files cannot be combined with source-tree options")
        try:
            report = convert_files(args.files, args.output, args.collision, args.overwrite)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(json.dumps(report, indent=2))
        return 0
    if args.root is None:
        parser.error("Use --files for new meshes or --root for an archived DAE/STL tree; use export_assets.py to package the current library")
    if args.collision or args.overwrite:
        parser.error("--collision and --overwrite require --files")
    root, output = args.root.resolve(), args.output.resolve()
    if output == root or root.is_relative_to(output):
        parser.error("Output must be a separate directory")
    if args.referenced_only and output.exists() and any(output.iterdir()):
        parser.error("--referenced-only requires an empty output directory")
    folders = [root / x for x in args.models] if args.models else [root / x for x in CATEGORIES]
    if any(not p.is_dir() or not p.resolve().is_relative_to(root) for p in folders):
        parser.error("Model directories must exist inside the source root")
    meshes = sorted({p for folder in folders for p in folder.rglob("*") if p.suffix.lower() in (".stl", ".dae")})
    urdfs = sorted({p for folder in folders for p in folder.rglob("*.urdf")})
    if not meshes:
        parser.error("No source meshes")
    references, visual_paths = {}, set()
    for urdf in urdfs:
        tree = ET.parse(str(urdf))
        for mesh in tree.findall(".//mesh"):
            if Path(mesh.get("filename", "")).suffix.lower() == ".glb":
                parser.error("Source already uses GLB; package it with scripts/export_assets.py")
            source = (urdf.parent / mesh.get("filename")).resolve()
            references.setdefault(source, []).append(urdf.relative_to(root).as_posix())
        for mesh in tree.findall(".//visual/geometry/mesh"):
            visual_paths.add((urdf.parent / mesh.get("filename")).resolve())
    if args.referenced_only:
        meshes = [p for p in meshes if p.resolve() in references]
    mappings = {}
    for path in meshes:
        relative = path.relative_to(root)
        # DAE takes the short name when a directory has both Link.stl/Link.dae.
        name = path.stem + (".stl.glb" if path.suffix == ".stl" and path.with_suffix(".dae").exists() else ".glb")
        parent = relative.parent.parent if relative.parent.name in ("stl", "dae") else relative.parent
        mappings[path.resolve()] = parent / "glb" / name
    if len(set(str(p).casefold() for p in mappings.values())) != len(mappings):
        parser.error("GLB output filenames collide")
    report = {"source_root": root.as_posix(), "asset_root": ".", "mesh_files": [], "urdfs": []}
    output.mkdir(parents=True, exist_ok=True)
    cache, failures = {}, []
    for count, path in enumerate(meshes, 1):
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        collision = path.resolve() not in visual_paths and ("Collision" in path.parts or "collision" in path.stem.lower())
        key = (digest, collision, path.name)
        try:
            if key in cache:
                cached_path, stats = cache[key]
                payload = cached_path.read_bytes()
            else:
                payload, stats = convert(path, collision)
            target = output / mappings[path.resolve()]
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".glb.tmp")
            temporary.write_bytes(payload)
            temporary.replace(target)
            cache[key] = (target, stats)
            report["mesh_files"].append({"source": relative, "glb": mappings[path.resolve()].as_posix(),
                                         "source_bytes": path.stat().st_size, "glb_bytes": len(payload),
                                         "source_sha256": digest, "glb_sha256": hashlib.sha256(payload).hexdigest(),
                                         "referenced_by": references.get(path.resolve(), []), **stats})
        except Exception as exc:
            failures.append(relative)
            report["mesh_files"].append({"source": relative, "error": str(exc)})
            print(f"ERROR {relative}: {exc}", flush=True)
        if count % 50 == 0:
            print(f"Converted {count}/{len(meshes)} meshes", flush=True)
        (output / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    if failures:
        print("URDF generation stopped because mesh conversion failed")
        return 1
    for urdf in urdfs:
        tree = ET.parse(str(urdf))
        for mesh in tree.findall(".//mesh"):
            source = (urdf.parent / mesh.get("filename")).resolve()
            target = mappings[source]
            mesh.set("filename", target.relative_to(urdf.parent.relative_to(root)).as_posix())
        target = output / urdf.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(ET.tostring(tree, encoding="utf-8", xml_declaration=True))
        report["urdfs"].append(urdf.relative_to(root).as_posix())
    report["summary"] = {"meshes": len(meshes), "urdfs": len(urdfs), "errors": 0,
                         "source_bytes": sum(f["source_bytes"] for f in report["mesh_files"]),
                         "glb_bytes": sum(f["glb_bytes"] for f in report["mesh_files"]),
                         "faces": sum(f["faces"] for f in report["mesh_files"])}
    (output / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"]))
    return 0


def convert_files(paths, output, collision=False, overwrite=False):
    """Validate the entire import before replacing any existing output files."""
    output = Path(output)
    pending, names = [], set()
    for path in paths:
        path = Path(path)
        if not path.is_file() or path.suffix.lower() not in (".dae", ".stl"):
            raise ValueError(f"Expected an existing DAE/STL file: {path}")
        name = path.stem + (".stl.glb" if path.suffix.lower() == ".stl" and path.with_suffix(".dae").exists() else ".glb")
        if name.casefold() in names:
            raise ValueError(f"Output filenames collide: {name}")
        names.add(name.casefold())
        target = output / name
        payload, stats = convert(path, collision)
        if target.exists() and target.read_bytes() != payload and not overwrite:
            raise ValueError(f"Existing GLB differs: {target}; review it before using --overwrite")
        pending.append((target, payload, {"source": str(path), "glb": str(target), **stats}))
    output.mkdir(parents=True, exist_ok=True)
    for target, payload, _ in pending:
        temporary = target.with_suffix(".glb.tmp")
        temporary.write_bytes(payload)
        temporary.replace(target)
    return [stats for _, _, stats in pending]


if __name__ == "__main__":
    raise SystemExit(main())
