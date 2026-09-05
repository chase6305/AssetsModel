"""Simplify dense STL/DAE meshes with measured acceptance checks.

Requires numpy, scipy, trimesh, rtree, pycollada and Blender 4.1+.
Original files are backed up before replacement. DAE materials, scene nodes,
units and transforms are retained; textured/skinned geometry is skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tempfile
from pathlib import Path

import collada
import numpy as np
import trimesh
from lxml import etree as ET

NS = "http://www.collada.org/2005/11/COLLADASchema"


def tag(name):
    return f"{{{NS}}}{name}"


def clean_mesh(vertices, faces):
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    if not np.isfinite(mesh.vertices).all():
        raise ValueError("Non-finite mesh vertices")
    # Weld only identical positions, without rounding away small features.
    vertices, inverse = np.unique(mesh.vertices, axis=0, return_inverse=True)
    mesh = trimesh.Trimesh(vertices, inverse[mesh.faces], process=False)
    mesh.update_faces(mesh.unique_faces() & mesh.nondegenerate_faces(height=1e-10))
    mesh.remove_unreferenced_vertices()
    return mesh


def topology(mesh):
    _, counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    return {"components": int(mesh.body_count),
            "boundary_edges": int(np.count_nonzero(counts == 1)),
            "nonmanifold_edges": int(np.count_nonzero(counts > 2))}


def probe_points(mesh, seed, samples):
    rng = np.random.default_rng(seed)
    vertices = mesh.vertices
    if len(vertices) > samples:
        vertices = vertices[rng.choice(len(vertices), samples, replace=False)]
    centers = mesh.triangles_center
    if len(centers) > samples:
        centers = centers[rng.choice(len(centers), samples, replace=False)]
    surface, _ = trimesh.sample.sample_surface(mesh, samples, seed=seed)
    return np.vstack((vertices, centers, surface))


def surface_distances(target, points):
    # Work at a common scale: trimesh's absolute triangle tolerances otherwise
    # misclassify submillimeter CAD triangles as edges, even against themselves.
    scale = 1000.0 / max(float(np.linalg.norm(target.extents)), 1e-12)
    origin = target.bounds.mean(axis=0)
    normalized = trimesh.Trimesh((target.vertices - origin) * scale, target.faces, process=False)
    points = (points - origin) * scale
    # Bound the temporary candidate-triangle arrays used by trimesh/rtree.
    return np.concatenate([
        trimesh.proximity.closest_point(normalized, points[i:i + 256])[1] / scale
        for i in range(0, len(points), 256)
    ])


def assess(original, candidate, tolerance, samples):
    before, after = topology(original), topology(candidate)
    metrics = {"topology_before": before, "topology_after": after}
    if not len(candidate.faces) or not np.isfinite(candidate.vertices).all():
        return False, {**metrics, "reason": "empty or non-finite geometry"}
    if after["components"] != before["components"]:
        return False, {**metrics, "reason": "component count changed"}
    for key in ("boundary_edges", "nonmanifold_edges"):
        if after[key] > before[key]:
            return False, {**metrics, "reason": f"increased {key}"}
    bounds_delta = float(np.max(np.abs(original.bounds - candidate.bounds)))
    area_delta = float(abs(candidate.area / original.area - 1))
    metrics.update(bounds_delta=bounds_delta, area_relative_delta=area_delta)
    if bounds_delta > tolerance or area_delta > 0.01:
        return False, {**metrics, "reason": "bounds or area deviation"}
    if original.is_watertight and abs(original.volume) > 1e-15:
        volume_delta = float(abs(candidate.volume / original.volume - 1))
        metrics["volume_relative_delta"] = volume_delta
        if volume_delta > 0.01:
            return False, {**metrics, "reason": "volume deviation"}
    distances = np.concatenate((
        surface_distances(candidate, probe_points(original, 17, samples)),
        surface_distances(original, probe_points(candidate, 29, samples)),
    ))
    metrics.update(sampled_max_distance=float(distances.max()),
                   sampled_p99_distance=float(np.quantile(distances, 0.99)),
                   sampled_rms_distance=float(np.sqrt(np.mean(distances ** 2))),
                   sample_count=len(distances), tolerance=tolerance)
    return metrics["sampled_max_distance"] <= tolerance, metrics


class BlenderWorker:
    def __init__(self, executable, scratch):
        self.scratch = scratch
        self.process = subprocess.Popen(
            [executable, "-b", "--factory-startup", "--threads", "2", "--python-exit-code", "1", "--python",
             str(Path(__file__).with_name("blender_mesh_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )

    def simplify(self, mesh, ratio):
        source, output = self.scratch / "input.npz", self.scratch / "output.npz"
        np.savez(source, vertices=mesh.vertices, faces=mesh.faces)
        self.process.stdin.write(json.dumps({"input": str(source), "output": str(output),
                                             "ratio": ratio}) + "\n")
        self.process.stdin.flush()
        for line in self.process.stdout:
            if line.startswith("ASSET_RESULT "):
                result = json.loads(line[len("ASSET_RESULT "):])
                if not result["ok"]:
                    raise RuntimeError(result["error"])
                with np.load(output) as data:
                    return (trimesh.Trimesh(data["vertices"], data["faces"], process=False),
                            data["normals"].copy())
        raise RuntimeError("Blender exited before returning a mesh")

    def close(self):
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        self.process.wait(timeout=60)


def simplify(mesh, worker, target, tolerance, samples):
    initial = min(1.0, target / len(mesh.faces))
    attempts = []
    for ratio in sorted({max(initial, step) for step in (0, 0.1, 0.2, 0.35, 0.5, 0.8)}):
        candidate, normals = worker.simplify(mesh, ratio)
        accepted, metrics = assess(mesh, candidate, tolerance, samples)
        attempts.append({"ratio": ratio, "faces": len(candidate.faces),
                         "accepted": bool(accepted), **metrics})
        if accepted:
            return candidate, normals, attempts
    # Retain the original geometry when a candidate fails the acceptance checks.
    candidate, normals = worker.simplify(mesh, 1.0)
    return candidate, normals, attempts


def consolidate_dae_vertices(mesh):
    """COLLADA requires one shared <vertices> element per <mesh>."""
    vertices = mesh.findall(tag("vertices"))
    if len(vertices) > 1:
        offsets, arrays, sources = {}, [], []
        offset = 0
        for vertex in vertices:
            position = vertex.find(f"{tag('input')}[@semantic='POSITION']")
            source = mesh.find(f"{tag('source')}[@id='{position.get('source')[1:]}']")
            array = source.find(tag("float_array"))
            values = np.fromstring(array.text, sep=" ").reshape(-1, 3)
            offsets["#" + vertex.get("id")] = offset
            arrays.append(values)
            sources.append(source)
            offset += len(values)
        values = np.vstack(arrays)
        array = sources[0].find(tag("float_array"))
        array.text = " ".join(format(float(x), ".9g") for x in values.flat)
        array.set("count", str(values.size))
        sources[0].find(f"{tag('technique_common')}/{tag('accessor')}").set("count", str(len(values)))
        for primitive in list(mesh):
            if primitive.tag not in (tag("triangles"), tag("lines")):
                continue
            inputs = primitive.findall(tag("input"))
            stride = max(int(x.get("offset", 0)) for x in inputs) + 1
            data = primitive.find(tag("p"))
            indices = np.fromstring(data.text, sep=" ", dtype=np.int64).reshape(-1, stride)
            for element in inputs:
                if element.get("semantic") == "VERTEX":
                    indices[:, int(element.get("offset", 0))] += offsets[element.get("source")]
                    element.set("source", "#" + vertices[0].get("id"))
            data.text = " ".join(str(int(x)) for x in indices.flat)
        for element in sources[1:] + vertices[1:]:
            mesh.remove(element)
    # Keep schema order: source arrays, a single vertices element, primitives.
    children = list(mesh)
    mesh[:] = [x for x in children if x.tag == tag("source")] + [
        x for x in children if x.tag == tag("vertices")
    ] + [x for x in children if x.tag not in (tag("source"), tag("vertices"))]


def replace_dae_geometry(xml_geometry, primitives, lines=()):
    old = xml_geometry.find(tag("mesh"))
    mesh = ET.Element(tag("mesh"))
    for index, (geometry, normals, material) in enumerate(primitives):
        prefix = f"{xml_geometry.get('id')}-optimized-{index}"
        normals, normal_indices = np.unique(np.round(normals.reshape(-1, 3), 6), axis=0, return_inverse=True)
        for suffix, values in [("positions", geometry.vertices), ("normals", normals)]:
            source = ET.SubElement(mesh, tag("source"), id=f"{prefix}-{suffix}")
            array = ET.SubElement(source, tag("float_array"), id=f"{prefix}-{suffix}-array",
                                  count=str(values.size))
            # Nine significant digits round-trip float32 mesh coordinates.
            array.text = " ".join(format(float(x), ".9g") for x in values.flat)
            common = ET.SubElement(source, tag("technique_common"))
            accessor = ET.SubElement(common, tag("accessor"),
                                    source=f"#{prefix}-{suffix}-array", count=str(len(values)), stride="3")
            for name in "XYZ":
                ET.SubElement(accessor, tag("param"), name=name, type="float")
        vertices = ET.SubElement(mesh, tag("vertices"), id=f"{prefix}-vertices")
        ET.SubElement(vertices, tag("input"), semantic="POSITION", source=f"#{prefix}-positions")
        attributes = {"count": str(len(geometry.faces))}
        if material:
            attributes["material"] = material
        triangles = ET.SubElement(mesh, tag("triangles"), **attributes)
        ET.SubElement(triangles, tag("input"), semantic="VERTEX", source=f"#{prefix}-vertices", offset="0")
        ET.SubElement(triangles, tag("input"), semantic="NORMAL", source=f"#{prefix}-normals", offset="1")
        indices = np.column_stack((geometry.faces.ravel(), normal_indices))
        ET.SubElement(triangles, tag("p")).text = " ".join(str(int(x)) for x in indices.flat)
    # CAD exports sometimes mix auxiliary line segments with triangle surfaces.
    # Preserve those segments exactly, without retaining unused vertex arrays.
    for index, (positions, indices, material) in enumerate(lines):
        values, inverse = np.unique(positions[indices].reshape(-1, 3), axis=0, return_inverse=True)
        prefix = f"{xml_geometry.get('id')}-lines-{index}"
        source = ET.SubElement(mesh, tag("source"), id=f"{prefix}-positions")
        array = ET.SubElement(source, tag("float_array"), id=f"{prefix}-array", count=str(values.size))
        array.text = " ".join(format(float(x), ".9g") for x in values.flat)
        common = ET.SubElement(source, tag("technique_common"))
        accessor = ET.SubElement(common, tag("accessor"), source=f"#{prefix}-array", count=str(len(values)), stride="3")
        for name in "XYZ":
            ET.SubElement(accessor, tag("param"), name=name, type="float")
        vertices = ET.SubElement(mesh, tag("vertices"), id=f"{prefix}-vertices")
        ET.SubElement(vertices, tag("input"), semantic="POSITION", source=f"#{prefix}-positions")
        segments = ET.SubElement(mesh, tag("lines"), count=str(len(indices)))
        if material:
            segments.set("material", material)
        ET.SubElement(segments, tag("input"), semantic="VERTEX", source=f"#{prefix}-vertices", offset="0")
        ET.SubElement(segments, tag("p")).text = " ".join(str(int(x)) for x in inverse)
    consolidate_dae_vertices(mesh)
    xml_geometry.replace(old, mesh)


def optimize_file(path, worker, args):
    is_collision = "Collision" in path.parts or "collision" in path.stem.lower()
    target = args.collision_target if is_collision else args.visual_target
    doc = None
    line_groups = {}
    if path.suffix.lower() == ".dae":
        xml = ET.parse(str(path), ET.XMLParser(huge_tree=True, resolve_entities=False))
        if xml.findall(f".//{tag('input')}[@semantic='TEXCOORD']") or xml.findall(f".//{tag('controller')}"):
            return None, {"status": "skipped", "reason": "textured or skinned DAE"}
        doc = collada.Collada(str(path))
        groups = []
        for geometry in doc.geometries:
            primitives = []
            for primitive in geometry.primitives:
                if isinstance(primitive, collada.lineset.LineSet):
                    line_groups.setdefault(geometry.id, []).append((primitive.vertex, primitive.vertex_index, primitive.material))
                    continue
                if isinstance(primitive, collada.polylist.Polylist):
                    primitive = primitive.triangleset()
                if not isinstance(primitive, collada.triangleset.TriangleSet):
                    return None, {"status": "skipped", "reason": "unsupported DAE primitive"}
                primitives.append((primitive.vertex, primitive.vertex_index, primitive.material))
            if primitives:
                groups.append((geometry.id, primitives))
    else:
        loaded = trimesh.load_mesh(path, process=False)
        groups = [(None, [(loaded.vertices, loaded.faces, None)])]
    total = sum(len(f) for _, primitives in groups for _, f, _ in primitives)
    if total <= args.threshold:
        return None, {"status": "below_threshold", "faces_before": total}
    unit = (doc.assetInfo.unitmeter or 1.0) if doc is not None else 1.0
    # Use only identity scene scales. Translations/rotations remain untouched.
    if doc is not None:
        for node in xml.findall(f".//{tag('node')}"):
            for element in node:
                if element.tag == tag("scale") and not np.allclose(np.fromstring(element.text, sep=" "), 1):
                    return None, {"status": "skipped", "reason": "nonidentity scene scale"}
                if element.tag == tag("matrix"):
                    matrix = np.fromstring(element.text, sep=" ").reshape(4, 4)[:3, :3]
                    if not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-5):
                        return None, {"status": "skipped", "reason": "scaled scene matrix"}
    stats, faces_after = [], 0
    for geometry_id, primitives in groups:
        replacements = []
        for vertices, faces, material in primitives:
            mesh = clean_mesh(vertices, faces)
            if not len(mesh.faces):
                return None, {"status": "skipped", "reason": "empty primitive after cleanup"}
            diagonal = float(np.linalg.norm(mesh.extents))
            tolerance = min(args.collision_error if is_collision else args.visual_error,
                            diagonal * unit * (0.0005 if is_collision else 0.001)) / unit
            budget = max(100, round(target * len(faces) / total))
            if len(mesh.faces) > budget:
                result, normals, attempts = simplify(mesh, worker, budget, tolerance, args.samples)
            else:
                result, normals = worker.simplify(mesh, 1.0)
                attempts = []
            faces_after += len(result.faces)
            stats.append({"geometry": geometry_id, "material": material,
                          "faces_before": len(faces), "faces_cleaned": len(mesh.faces),
                          "faces_after": len(result.faces), "attempts": attempts})
            replacements.append((result, normals, material))
        if doc is not None:
            element = xml.find(f".//{tag('geometry')}[@id='{geometry_id}']")
            replace_dae_geometry(element, replacements, line_groups.get(geometry_id, []))
    if doc is not None:
        payload = ET.tostring(xml, xml_declaration=True, encoding="utf-8")
        # A successful geometric check must also survive serialization/reload.
        collada.Collada(io.BytesIO(payload))
    else:
        payload = result.export(file_type="stl")
    if len(payload) >= path.stat().st_size or faces_after >= total:
        return None, {"status": "retained", "faces_before": total, "faces_after": total,
                      "reason": "no accepted size and face reduction", "primitives": stats}
    return payload, {"status": "optimized", "faces_before": total,
                     "faces_after": faces_after, "primitives": stats}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("CameraModel"), Path("RobotModel"), Path("ToolModel")])
    parser.add_argument("--apply", action="store_true", help="Write accepted meshes; default is analysis only")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path, default=Path("mesh-optimization.json"))
    parser.add_argument("--blender", default="blender")
    parser.add_argument("--threshold", type=int, default=20000)
    parser.add_argument("--visual-target", type=int, default=20000)
    parser.add_argument("--collision-target", type=int, default=10000)
    parser.add_argument("--visual-error", type=float, default=0.0005, help="Maximum sampled deviation in meters")
    parser.add_argument("--collision-error", type=float, default=0.00025)
    parser.add_argument("--samples", type=int, default=3000, help="Samples per probe class and direction")
    args = parser.parse_args()
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    if min(args.threshold, args.visual_target, args.collision_target, args.samples,
           args.visual_error, args.collision_error) <= 0:
        parser.error("Targets, thresholds, samples and tolerances must be positive")
    root = Path.cwd().resolve()
    files = sorted({p.resolve() for entry in args.paths
                    for p in ([entry] if entry.is_file() else entry.rglob("*"))
                    if p.suffix.lower() in (".stl", ".dae")})
    if not files:
        parser.error("No STL or DAE meshes found")
    report = {"settings": {k: str(v) if isinstance(v, Path) else v
                            for k, v in vars(args).items() if k != "paths"}, "files": []}
    cache = {}
    failures = 0
    with tempfile.TemporaryDirectory(prefix="assets-mesh-") as temp:
        scratch = Path(temp)
        worker = BlenderWorker(args.blender, scratch)
        try:
            for path in files:
                relative = path.relative_to(root)
                original = path.read_bytes()
                digest = hashlib.sha256(original).hexdigest()
                key = (digest, "Collision" in path.parts or "collision" in path.stem.lower())
                try:
                    if key in cache:
                        cached_path, details = cache[key]
                        payload = cached_path.read_bytes() if cached_path else None
                    else:
                        payload, details = optimize_file(path, worker, args)
                        cached_path = scratch / f"cache-{len(cache)}{path.suffix}" if payload else None
                        if cached_path:
                            cached_path.write_bytes(payload)
                        cache[key] = (cached_path, details)
                    entry = {"path": relative.as_posix(), "bytes_before": len(original),
                             "bytes_after": len(payload) if payload else len(original),
                             "sha256_before": digest,
                             "sha256_after": hashlib.sha256(payload or original).hexdigest(), **details}
                    if payload and args.apply:
                        backup = args.backup_dir.resolve() / relative
                        if backup.resolve() == path:
                            raise ValueError("Backup directory resolves to the source tree")
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        if backup.exists():
                            if backup.read_bytes() != original:
                                raise ValueError("Backup exists with different contents; choose a new backup directory")
                        else:
                            with backup.open("xb") as stream:
                                stream.write(original)
                        staging = path.with_suffix(path.suffix + ".tmp")
                        staging.write_bytes(payload)
                        staging.replace(path)
                    if details["status"] not in ("below_threshold",):
                        print(f"{relative}: {details['status']} {details.get('faces_before', '?')} -> {details.get('faces_after', '?')}", flush=True)
                except Exception as exc:
                    failures += 1
                    entry = {"path": relative.as_posix(), "status": "error", "error": str(exc)}
                    print(f"{relative}: ERROR {exc}", flush=True)
                report["files"].append(entry)
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, indent=2) + "\n")
        finally:
            worker.close()
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
