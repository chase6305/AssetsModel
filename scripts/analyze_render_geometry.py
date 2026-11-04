"""Read-only diagnosis of robot visual GLB shading and draw-call structure.

Reports exact coincident-face duplicates, opposing corner normals and sharp
normal seams across nearly coplanar adjacent triangles. These are review
candidates, not automatic repair instructions; CAD boundaries may be intentional.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from glb_format import accessor_bytes, read_glb

ROOT = Path(__file__).resolve().parents[1]
DTYPES = {'b': 'i1', 'B': 'u1', 'h': '<i2', 'H': '<u2', 'I': '<u4', 'f': '<f4'}


def values(tree, binary, index):
    raw, component, width = accessor_bytes(tree, binary, index)
    return np.frombuffer(raw, dtype=DTYPES[component]).reshape(-1, width)


def visible_meshes(tree):
    scene = tree.get('scenes', [])[tree.get('scene', 0)]
    pending, counts = list(scene.get('nodes', [])), Counter()
    while pending:
        node = tree['nodes'][pending.pop()]
        if 'mesh' in node:
            counts[node['mesh']] += 1
        pending.extend(node.get('children', []))
    return counts


def edge_findings(welded_faces, face_normals, corner_normals):
    count = len(welded_faces)
    edges = np.vstack((welded_faces[:, [0, 1]], welded_faces[:, [1, 2]], welded_faces[:, [2, 0]]))
    edge_corners = np.concatenate((corner_normals[:, [0, 1]], corner_normals[:, [1, 2]], corner_normals[:, [2, 0]]))
    swapped = edges[:, 0] > edges[:, 1]
    canonical = np.sort(edges, axis=1)
    edge_corners[swapped] = edge_corners[swapped, ::-1]
    order = np.lexsort((canonical[:, 1], canonical[:, 0]))
    sorted_edges = canonical[order]
    starts = np.r_[0, np.flatnonzero(np.any(np.diff(sorted_edges, axis=0), axis=1)) + 1]
    sizes = np.diff(np.r_[starts, len(order)])
    pairs = starts[sizes == 2]
    a, b = order[pairs], order[pairs + 1]
    face_ids = np.tile(np.arange(count), 3)
    face_dot = np.einsum('ij,ij->i', face_normals[face_ids[a]], face_normals[face_ids[b]])
    normal_dot = np.einsum('ijk,ijk->ij', edge_corners[a], edge_corners[b])
    valid = (np.linalg.norm(edge_corners[a], axis=2).min(axis=1) > .5) & (np.linalg.norm(edge_corners[b], axis=2).min(axis=1) > .5)
    suspicious = valid & (face_dot > np.cos(np.deg2rad(5))) & (normal_dot.min(axis=1) < np.cos(np.deg2rad(15)))
    winding = np.all(edges[a] == edges[b], axis=1)
    return {'boundary_edges': int(np.count_nonzero(sizes == 1)),
            'nonmanifold_edges': int(np.count_nonzero(sizes > 2)),
            'shared_manifold_edges': len(pairs), 'same_direction_manifold_edges': int(np.count_nonzero(winding)),
            'normal_seam_edges_on_near_coplanar_faces': int(np.count_nonzero(suspicious)),
            'normal_seam_edge_examples_welded_vertices': canonical[a[suspicious]][:12].tolist()}


def audit(path):
    raw = path.read_bytes()
    tree, binary = read_glb(raw)
    meshes = visible_meshes(tree)
    positions, faces, corner_normals, face_materials = [], [], [], []
    parts, vertex_offset, draws = [], 0, 0
    materials_by_semantics = Counter()
    for mi, instances in sorted(meshes.items()):
        for pi, primitive in enumerate(tree['meshes'][mi]['primitives']):
            if primitive.get('mode', 4) != 4:
                continue
            vertices = values(tree, binary, primitive['attributes']['POSITION']).astype(np.float64)
            indices = values(tree, binary, primitive['indices']).ravel().astype(np.int64).reshape(-1, 3)
            part = {'mesh': mi, 'primitive': pi, 'vertices': len(vertices), 'triangles': len(indices),
                    'material': primitive.get('material'), 'instances': instances}
            normals = np.zeros((len(vertices), 3), dtype=float)
            if 'NORMAL' in primitive['attributes']:
                normals = values(tree, binary, primitive['attributes']['NORMAL']).astype(np.float64)
                lengths = np.linalg.norm(normals, axis=1)
                part['zero_normals'] = int(np.count_nonzero(lengths < 1e-12))
                part['non_unit_normals'] = int(np.count_nonzero(np.abs(lengths - 1) > 1e-3))
                part['non_finite_normals'] = int(np.count_nonzero(~np.isfinite(normals).all(axis=1)))
                normals /= np.maximum(lengths[:, None], 1e-15)
            else:
                part['missing_normals'] = True
            part['unreferenced_vertices'] = len(vertices) - len(np.unique(indices))
            material = tree.get('materials', [])[primitive['material']] if 'material' in primitive else {}
            semantic = {k: v for k, v in material.items() if k not in ('name', 'extras')}
            materials_by_semantics[json.dumps(semantic, sort_keys=True)] += instances
            positions.append(vertices)
            faces.append(indices + vertex_offset)
            corner_normals.append(normals[indices])
            face_materials.extend([primitive.get('material', -1)] * len(indices))
            vertex_offset += len(vertices)
            draws += instances
            parts.append(part)
    vertices, faces, normals = np.concatenate(positions), np.concatenate(faces), np.concatenate(corner_normals)
    triangles = vertices[faces]
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    area2 = np.linalg.norm(cross, axis=1)
    face_normals = cross / np.maximum(area2[:, None], 1e-30)
    corner_dot = np.einsum('ijk,ik->ij', normals, face_normals)
    valid = area2 > 1e-20
    opposite = valid & (corner_dot.mean(axis=1) < -.2)
    severe = valid & (corner_dot.min(axis=1) < -.2)
    _, welded = np.unique(vertices, axis=0, return_inverse=True)
    welded_faces = welded[faces]
    sorted_faces = np.sort(welded_faces, axis=1)
    _, first, groups, multiplicity = np.unique(sorted_faces, axis=0, return_index=True, return_inverse=True, return_counts=True)
    duplicate_groups = np.flatnonzero(multiplicity > 1)
    material_ids = np.asarray(face_materials)
    cross_material_duplicates = 0
    for group in duplicate_groups:
        if len(np.unique(material_ids[groups == group])) > 1:
            cross_material_duplicates += 1
    result = {'sha256': hashlib.sha256(raw).hexdigest(), 'bin_sha256': hashlib.sha256(binary).hexdigest(),
              'default_scene_draw_calls': draws, 'semantic_material_groups': len(materials_by_semantics),
              'potential_draw_call_reduction_same_material': draws - len(materials_by_semantics),
              'triangles': len(faces), 'vertices': len(vertices), 'unique_positions': int(welded.max()) + 1,
              'zero_area_triangles': int(np.count_nonzero(area2 == 0)),
              'near_zero_area_triangles': int(np.count_nonzero(area2 <= 1e-20)),
              'duplicate_coordinate_triangles': int(np.sum(multiplicity - 1)),
              'cross_material_duplicate_face_groups': cross_material_duplicates,
              'duplicate_faces_same_winding': int(np.count_nonzero((np.arange(len(faces)) != first[groups]) & (np.einsum('ij,ij->i', face_normals, face_normals[first[groups]]) > .999))),
              'duplicate_faces_opposite_winding': int(np.count_nonzero((np.arange(len(faces)) != first[groups]) & (np.einsum('ij,ij->i', face_normals, face_normals[first[groups]]) < -.999))),
              'opposing_corner_normal_surface_area_fraction': float(area2[opposite].sum() / max(area2.sum(), 1e-30)),
              'opposing_face_area_median_relative_to_all_faces': float(np.median(area2[opposite]) / max(np.median(area2[valid]), 1e-30)) if np.any(opposite) else 0.0,
              'nodes_with_transform_fields': sum(any(k in node for k in ('matrix', 'translation', 'rotation', 'scale')) for node in tree.get('nodes', [])),
              'faces_mean_corner_normal_opposes_winding': int(np.count_nonzero(opposite)),
              'faces_any_corner_normal_opposes_winding': int(np.count_nonzero(severe)),
              'opposing_face_examples': np.flatnonzero(opposite)[:16].tolist(),
              'primitives': parts}
    result.update(edge_findings(welded_faces, face_normals, normals))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--json', type=Path, required=True)
    parser.add_argument('--files', nargs='+', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    paths = [(root / p).resolve() for p in args.files] if args.files else sorted((root / 'RobotModel').glob('*/*/Visual/glb/*.glb'))
    if args.baseline:
        paths = [p for p in paths if (args.baseline / p.relative_to(root)).exists()]
    records = []
    for path in paths:
        if not path.is_relative_to(root):
            parser.error('Input must be inside root')
        record = {'path': path.relative_to(root).as_posix()}
        try:
            record.update(audit(path))
            if args.baseline and (args.baseline / path.relative_to(root)).exists():
                _, original_bin = read_glb(args.baseline / path.relative_to(root))
                record['bin_identical_to_baseline'] = hashlib.sha256(original_bin).hexdigest() == record['bin_sha256']
        except Exception as exc:
            record['error'] = str(exc)
        records.append(record)
        print(f"{record['path']}: {record.get('triangles', record.get('error'))}", flush=True)
    keys = ['triangles', 'vertices', 'default_scene_draw_calls', 'potential_draw_call_reduction_same_material',
            'duplicate_coordinate_triangles', 'duplicate_faces_same_winding', 'duplicate_faces_opposite_winding', 'zero_area_triangles', 'faces_mean_corner_normal_opposes_winding',
            'normal_seam_edges_on_near_coplanar_faces', 'same_direction_manifold_edges']
    summary = {'visual_glbs': len(records), 'errors': sum('error' in r for r in records),
               **{key: sum(r.get(key, 0) for r in records) for key in keys}}
    report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'summary': summary,
              'interpretation': 'Exact coordinate welding; candidate defects require visual/mechanical review. Material grouping reduction estimates do not authorize merging separate scene nodes.',
              'files': records}
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(summary), flush=True)
    return int(summary['errors'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
