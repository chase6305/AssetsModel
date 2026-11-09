"""Read-only audit of GLB surface normals, material factors, and triangle quality.

This reports existing geometry; it never modifies assets or interprets aesthetic
warnings as certified collision/dynamics defects.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from glb_format import read_glb
from prepare_preview import accessor_array


def audit(path):
    tree, binary = read_glb(path)
    triangles = missing_normals = zero_normals = off_unit_normals = inverted_corners = degenerate = 0
    max_normal_length_error = 0.0
    min_area = float('inf')
    material_records = []
    for material in tree.get('materials', []):
        pbr = material.get('pbrMetallicRoughness', {})
        color = pbr.get('baseColorFactor', [1, 1, 1, 1])
        metallic, roughness = pbr.get('metallicFactor', 1), pbr.get('roughnessFactor', 1)
        if len(color) != 4 or not np.isfinite(color).all() or not all(0 <= x <= 1 for x in color):
            raise ValueError('Invalid linear baseColorFactor')
        if not all(np.isfinite(x) and 0 <= x <= 1 for x in (metallic, roughness)):
            raise ValueError('Invalid metallic/roughness factor')
        material_records.append({'name': material.get('name', ''), 'color_linear': color,
                                 'metallic': metallic, 'roughness': roughness,
                                 'textured': any('Texture' in k for k in pbr) or any('Texture' in k for k in material),
                                 'double_sided': material.get('doubleSided', False)})
    normal_lengths = []
    for mesh in tree.get('meshes', []):
        for primitive in mesh['primitives']:
            if primitive.get('mode', 4) != 4:
                continue
            attrs = primitive['attributes']
            vertices = accessor_array(tree, binary, attrs['POSITION'])
            faces = (accessor_array(tree, binary, primitive['indices']).reshape(-1, 3)
                     if 'indices' in primitive else np.arange(len(vertices)).reshape(-1, 3))
            if not np.isfinite(vertices).all():
                raise ValueError('Non-finite positions')
            if faces.min() < 0 or faces.max() >= len(vertices):
                raise ValueError('Invalid triangle indices')
            corners = vertices[faces].astype(np.float64)
            cross = np.cross(corners[:, 1]-corners[:, 0], corners[:, 2]-corners[:, 0])
            lengths = np.linalg.norm(cross, axis=1)
            valid = lengths > 1e-20
            if valid.any():
                min_area = min(min_area, float(lengths[valid].min() / 2))
            triangles += len(faces)
            degenerate += int((~valid).sum())
            if 'NORMAL' not in attrs:
                missing_normals += len(faces)
                continue
            normals = accessor_array(tree, binary, attrs['NORMAL'])
            if normals.shape != vertices.shape or not np.isfinite(normals).all():
                raise ValueError('Invalid/non-finite NORMAL attribute')
            normal_length = np.linalg.norm(normals, axis=1)
            normal_lengths.append(normal_length)
            zero_normals += int((normal_length < 1e-12).sum())
            off_unit_normals += int((np.abs(normal_length - 1) > 1e-3).sum())
            max_normal_length_error = max(max_normal_length_error, float(np.abs(normal_length-1).max()))
            face_normals = np.divide(cross, lengths[:, None], out=np.zeros_like(cross), where=valid[:, None])
            dot = np.einsum('fc,fvc->fv', face_normals, normals[faces])
            inverted_corners += int((dot[valid] < -1e-4).sum())
    if not triangles:
        raise ValueError('No triangle surfaces')
    return {'triangles': triangles, 'degenerate_triangles': degenerate,
            'triangles_without_authored_normals': missing_normals, 'zero_normal_vertices': zero_normals,
            'off_unit_normal_vertices': off_unit_normals, 'max_normal_length_error': max_normal_length_error,
            'opposed_normal_corners': inverted_corners,
            'minimum_valid_triangle_area_local': None if min_area == float('inf') else min_area,
            'materials': material_records,
            'embedded_images': len(tree.get('images', []))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--json', type=Path, required=True)
    parser.add_argument('--all-categories', action='store_true', help='Include camera/tool visual meshes')
    args = parser.parse_args()
    records = []
    categories = ('RobotModel', 'CameraModel', 'ToolModel') if args.all_categories else ('RobotModel',)
    for category in categories:
        for path in sorted((args.root / category).rglob('*.glb')):
            if 'Collision' in path.parts or 'collision' in path.stem.lower() or 'coarse' in path.stem.lower():
                continue
            record = {'path': path.relative_to(args.root).as_posix()}
            try:
                record.update(audit(path))
            except Exception as exc:
                record['error'] = str(exc)
                print(f"ERROR {record['path']}: {exc}", flush=True)
            records.append(record)
    keys = ('triangles', 'degenerate_triangles', 'triangles_without_authored_normals',
            'zero_normal_vertices', 'off_unit_normal_vertices', 'opposed_normal_corners')
    summary = {'files': len(records), 'errors': sum('error' in r for r in records),
               **{key: sum(r.get(key, 0) for r in records) for key in keys},
               'textured_materials': sum(m['textured'] for r in records for m in r.get('materials', []))}
    report = {'summary': summary, 'scope': 'visual GLB resources, collision/coarse resources excluded',
              'notes': ['Counts refer to authored attributes and local surface winding; no meshes are rewritten.',
                        'Opposed corners can reflect source CAD winding or smoothing; inspect renders before changing normals.',
                        'Missing authored normals use flat face normals in the preview; no smooth surface is invented.'],
              'meshes': records}
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(summary), flush=True)
    return int(not records or summary['errors'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
