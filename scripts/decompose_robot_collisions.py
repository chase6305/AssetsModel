#!/usr/bin/env python3
"""Replace legacy robot collision meshes with coarse CoACD convex OBJ files."""

import argparse
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LEGACY_FORMATS = {".glb", ".stl"}
COLLISION_BLOCK = re.compile(rb"<!--.*?-->|<collision\b[^>]*>.*?</collision\s*>", re.DOTALL)


def collision_source(urdf, collision):
    mesh = collision.find("geometry/mesh")
    if mesh is None or Path(mesh.get("filename", "")).suffix.lower() not in LEGACY_FORMATS:
        return None
    path = (urdf.parent / mesh.get("filename")).resolve()
    if not path.is_relative_to(urdf.parent / "Collision") or not path.is_file():
        raise ValueError(f"{urdf}: expected an existing collision source in Collision/: {path}")
    return path


def rewrite_urdf(urdf, original, outputs):
    tree = ET.fromstring(original)
    collisions = [collision for link in tree.findall("link") for collision in link.findall("collision")]
    blocks = [match for match in COLLISION_BLOCK.finditer(original) if not match[0].startswith(b"<!--")]
    if [ET.tostring(ET.fromstring(match[0])) for match in blocks] != [ET.tostring(c).rstrip() for c in collisions]:
        raise ValueError(f"{urdf}: collision blocks do not match parsed URDF")

    updated = original
    for match, collision in reversed(list(zip(blocks, collisions))):
        source = collision_source(urdf, collision)
        if source is None:
            continue
        mesh = collision.find("geometry/mesh")
        filename = mesh.get("filename")
        name = collision.get("name")
        filename_attr = re.compile(rb"(\bfilename\s*=\s*)(['\"])" + re.escape(filename.encode()) + rb"\2")
        if len(filename_attr.findall(match[0])) != 1:
            raise ValueError(f"{urdf}: expected one filename attribute for {filename}")
        indent = re.match(rb"[ \t]*", original[original.rfind(b"\n", 0, match.start()) + 1:])[0]
        line_break = b"\r\n" if b"\r\n" in original else b"\n"
        parts = []
        for index, output in enumerate(outputs[source], 1):
            relative = output.relative_to(urdf.parent).as_posix().encode()
            block = filename_attr.sub(lambda m: m[1] + m[2] + relative + m[2], match[0])
            if index > 1 and name:
                name_attr = re.compile(rb"(<collision\b[^>]*?\bname\s*=\s*)(['\"])" + re.escape(name.encode()) + rb"\2")
                renamed = f"{name}_hull_{index}".encode()
                block = name_attr.sub(lambda m: m[1] + m[2] + renamed + m[2], block, count=1)
            parts.append(block)
        updated = updated[:match.start()] + (line_break + indent).join(parts) + updated[match.end():]
    return updated


def write_asset(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".coacd-", dir=path.parent) as directory:
        staged = Path(directory) / path.name
        staged.write_bytes(data)
        staged.replace(path)


def decompose(source, tool, threshold, max_hulls, max_vertices, merge_vert_ratio):
    mesh = tool.trimesh.load_scene(source, process=False).to_mesh()
    legacy = tool.o3d.geometry.TriangleMesh(
        tool.o3d.utility.Vector3dVector(mesh.vertices),
        tool.o3d.utility.Vector3iVector(mesh.faces),
    )
    _, cleaned = tool.MeshProcessor.vertices_fixing_util(legacy, merge_vert_ratio)
    coacd_mesh = tool.coacd.Mesh(tool.np.asarray(cleaned.vertices), tool.np.asarray(cleaned.triangles))
    parts = tool.coacd.run_coacd(coacd_mesh, threshold=threshold, max_convex_hull=max_hulls,
                                resolution=1000, mcts_nodes=12, mcts_iterations=48, mcts_max_depth=2,
                                decimate=True, max_ch_vertex=max_vertices,
                                extrude=True, extrude_margin=0.00005)
    if not 1 <= len(parts) <= max_hulls:
        raise ValueError(f"{source}: invalid CoACD output")

    hulls = []
    for vertices, _ in parts:
        hull = tool.trimesh.convex.convex_hull(vertices)
        if not hull.is_volume or len(hull.vertices) > max_vertices:
            raise ValueError(f"{source}: expected a solid convex hull within the vertex budget")
        hulls.append(hull)
    hulls.sort(key=lambda hull: (-hull.volume, *hull.centroid))
    bounds = tool.trimesh.util.concatenate(hulls).bounds
    record = {"source_faces": len(mesh.faces), "hulls": len(hulls),
              "obj_faces": sum(len(hull.faces) for hull in hulls),
              "bounds_delta": (bounds - mesh.bounds).tolist()}
    return hulls, record


def run(root, folders, tool, settings):
    urdfs = {p: p.read_bytes() for folder in folders for p in sorted(folder.glob("*.urdf"))}
    sources = sorted({source for urdf, data in urdfs.items()
                      for collision in ET.fromstring(data).findall("link/collision")
                      if (source := collision_source(urdf, collision)) is not None})
    outputs, objects, records = {}, {}, []
    for index, source in enumerate(sources, 1):
        collision_dir = next(parent for parent in source.parents if parent.name == "Collision")
        hulls, record = decompose(source, tool, **settings)
        outputs[source] = []
        for part, hull in enumerate(hulls, 1):
            stem = source.stem if part == 1 else f"{source.stem}_hull_{part}"
            obj = collision_dir / "obj" / f"{stem}.obj"
            obj_text = hull.export(file_type="obj", include_normals=False, include_color=False,
                                   include_texture=False, digits=10)
            payload = (obj_text.rstrip("\n") + "\n").encode()
            if obj in objects or (obj.exists() and obj.read_bytes() != payload):
                raise ValueError(f"Refusing to overwrite an existing OBJ: {obj}")
            objects[obj] = payload
            outputs[source].append(obj)
        records.append({"source": source.relative_to(root).as_posix(), **record})
        print(f"[{index}/{len(sources)}] {records[-1]['source']} -> {len(hulls)} OBJ", flush=True)

    updates = {urdf: rewrite_urdf(urdf, data, outputs) for urdf, data in urdfs.items()}
    referenced = {(urdf.parent / resource.get("filename", "")).resolve()
                  for urdf, data in updates.items() for resource in ET.fromstring(data).iter()
                  if resource.tag in ("mesh", "texture")}
    legacy = sorted({path for folder in folders for path in (folder / "Collision").rglob("*")
                     if path.is_file() and path.suffix.lower() in LEGACY_FORMATS})
    if set(legacy) & referenced:
        raise ValueError("A legacy Collision/ GLB or STL is still referenced")
    for obj, data in objects.items():
        write_asset(obj, data)
    for urdf, data in updates.items():
        if data != urdfs[urdf]:
            write_asset(urdf, data)
    for path in legacy:
        path.unlink()
    for directory in {path.parent for path in legacy if path.parent.name in ("glb", "stl")}:
        if not any(directory.iterdir()):
            directory.rmdir()
    return {"parameters": settings,
            "urdfs_updated": sum(data != urdfs[urdf] for urdf, data in updates.items()),
            "source_meshes": len(sources), "objs": len(objects),
            "removed_legacy_meshes": len(legacy), "hulls_per_mesh": dict(Counter(x["hulls"] for x in records)),
            "source_faces": sum(x["source_faces"] for x in records),
            "obj_faces": sum(x["obj_faces"] for x in records), "meshes": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--root", type=Path, default=REPO)
    parser.add_argument("--coacd-dir", type=Path, required=True, help="Directory containing coacd_process.py")
    parser.add_argument("--models", nargs="+", type=Path, help="Model directories relative to RobotModel/")
    parser.add_argument("--threshold", type=float, default=0.18, help="CoACD concavity threshold")
    parser.add_argument("--max-hulls", type=int, default=3, help="Maximum convex hulls per input mesh")
    parser.add_argument("--max-vertices", type=int, default=64, help="Maximum vertices per convex hull")
    parser.add_argument("--merge-vert-ratio", type=float, default=0.001, help="Vertex merge distance relative to mesh size")
    parser.add_argument("--report", type=Path, help="Optional JSON report with hull counts, triangles and bounds deltas")
    args = parser.parse_args()
    if not 0 < args.threshold <= 1 or args.max_hulls < 1 or args.max_vertices < 8 or not 0 <= args.merge_vert_ratio < 1:
        parser.error("Expected threshold in (0, 1], at least one hull and eight vertices, and merge ratio in [0, 1)")
    root = args.root.resolve()
    robots = root / "RobotModel"
    folders = (sorted({(robots / name).resolve() for name in args.models}) if args.models else
               sorted({path.parent for path in robots.rglob("*.urdf")}))
    if not folders or any(not folder.is_relative_to(robots) or not list(folder.glob("*.urdf")) for folder in folders):
        parser.error("Select directories containing URDF files under RobotModel/")
    coacd_dir = args.coacd_dir.resolve()
    if not (coacd_dir / "coacd_process.py").is_file():
        parser.error(f"Missing coacd_process.py in {coacd_dir}")
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    sys.path.insert(0, str(coacd_dir))
    import coacd_process as tool

    tool.coacd.set_log_level("error")
    settings = {name: getattr(args, name) for name in ("threshold", "max_hulls", "max_vertices", "merge_vert_ratio")}
    report = run(root, folders, tool, settings)
    print(json.dumps({key: value for key, value in report.items() if key != "meshes"}, sort_keys=True))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
