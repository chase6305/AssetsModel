"""Build review copies that smooth only fully flat-authored visual primitives.

Positions, triangle order/winding, materials, node transforms and scene membership
stay exact. Existing smooth normal groups are retained. Outputs never overwrite
source assets; use a separate root for the review copies.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

import numpy as np

from convert_glb import compact, smooth_stl_normals
from glb_format import accessor_bytes, read_glb, validate_glb
from prepare_preview import accessor_array
from refine_materials import encode_glb

ROOT = Path(__file__).resolve().parents[1]


def prune_accessors(tree, binary):
    """Repack referenced attributes without retaining old unused normal buffers."""
    target = copy.deepcopy(tree)
    target['accessors'], target['bufferViews'] = [], []
    packed, remap = bytearray(), {}

    def remap_accessor(old, target_kind):
        key = (old, target_kind)
        if key in remap:
            return remap[key]
        original = tree['accessors'][old]
        raw, _, _ = accessor_bytes(tree, binary, old)
        stride = len(raw)//original['count']
        padded_stride = (stride+3)//4*4 if target_kind == 34962 else stride
        if padded_stride != stride:
            rows = np.frombuffer(raw, dtype=np.uint8).reshape(original['count'], stride)
            padded = np.zeros((original['count'], padded_stride), dtype=np.uint8)
            padded[:, :stride] = rows
            raw = padded.tobytes()
        packed.extend(b'\0' * (-len(packed) % 4))
        vi = len(target['bufferViews'])
        view = {'buffer': 0, 'byteOffset': len(packed), 'byteLength': len(raw), 'target': target_kind}
        if padded_stride != stride:
            view['byteStride'] = padded_stride
        target['bufferViews'].append(view)
        packed.extend(raw)
        accessor = copy.deepcopy(original)
        accessor['bufferView'] = vi
        accessor.pop('byteOffset', None)
        new = len(target['accessors'])
        target['accessors'].append(accessor)
        remap[key] = new
        return new

    for mesh in target['meshes']:
        for primitive in mesh['primitives']:
            primitive['attributes'] = {name: remap_accessor(ai, 34962) for name, ai in primitive['attributes'].items()}
            primitive['indices'] = remap_accessor(primitive['indices'], 34963)
    target['buffers'] = [{'byteLength': len(packed)}]
    packed.extend(b'\0' * (-len(packed) % 4))
    return target, bytes(packed)


def refine(tree, binary, angle=30.0, smoother=None, preserve_direction_outliers=False, eligible_primitives=None):
    smoother = smooth_stl_normals if smoother is None else smoother
    selected = None if eligible_primitives is None else frozenset(tuple(pair) for pair in eligible_primitives)
    available = {(mi, pi) for mi, mesh in enumerate(tree['meshes']) for pi, _ in enumerate(mesh['primitives'])}
    if selected is not None and not selected <= available:
        raise ValueError('Selected primitive does not exist in this GLB')
    for key in ('skins', 'animations', 'images', 'textures', 'extensions', 'extensionsUsed', 'extensionsRequired'):
        if tree.get(key):
            raise ValueError(f'Unsupported {key}; retain authored shading')
    if len(tree.get('buffers', [])) != 1 or set(tree['buffers'][0]) - {'byteLength'}:
        raise ValueError('Buffer metadata beyond the embedded byte layout requires separate review')
    for view in tree['bufferViews']:
        if set(view) - {'buffer', 'byteOffset', 'byteLength', 'byteStride', 'target'}:
            raise ValueError('Buffer view metadata beyond the byte layout requires separate review')
    for accessor in tree['accessors']:
        if set(accessor) - {'bufferView', 'byteOffset', 'componentType', 'count', 'type', 'normalized', 'min', 'max'}:
            raise ValueError('Accessor metadata beyond the byte layout requires separate review')
    for mesh in tree['meshes']:
        if 'weights' in mesh or 'extensions' in mesh:
            raise ValueError('Morph weights or extended mesh semantics require separate review')
        for primitive in mesh['primitives']:
            if 'targets' in primitive or 'extensions' in primitive:
                raise ValueError('Morph targets or extended primitive semantics require separate review')
            if 'indices' not in primitive:
                raise ValueError('Non-indexed primitive needs a separate attribute-preserving path')
    for node in tree.get('nodes', []):
        if 'weights' in node or 'skin' in node or 'extensions' in node:
            raise ValueError('Morphing, skinned or extended node semantics require separate review')
    result, packed = copy.deepcopy(tree), bytearray(binary)
    edits = []
    protected_masks = {}

    def append_accessor(values, component, kind, position=False):
        values = np.ascontiguousarray(values)
        packed.extend(b'\0' * (-len(packed) % 4))
        vi = len(result['bufferViews'])
        result['bufferViews'].append({'buffer': 0, 'byteOffset': len(packed), 'byteLength': values.nbytes})
        packed.extend(values.tobytes())
        accessor = {'bufferView': vi, 'componentType': component, 'type': kind, 'count': len(values)}
        if position:
            accessor.update(min=values.min(axis=0).tolist(), max=values.max(axis=0).tolist())
        ai = len(result['accessors'])
        result['accessors'].append(accessor)
        return ai

    for mi, mesh in enumerate(tree['meshes']):
        for pi, primitive in enumerate(mesh['primitives']):
            if primitive.get('mode', 4) != 4:
                continue
            record = {'mesh': mi, 'primitive': pi, 'material': primitive.get('material')}
            if selected is not None and (mi, pi) not in selected:
                edits.append({**record, 'status': 'retained', 'reason': 'outside selected primitive scope'})
                continue
            attributes = primitive['attributes']
            if set(attributes) != {'POSITION', 'NORMAL'} or 'indices' not in primitive:
                edits.append({**record, 'status': 'retained', 'reason': 'unsupported or unauthored attributes'})
                continue
            vertices = accessor_array(tree, binary, attributes['POSITION'])
            faces = accessor_array(tree, binary, primitive['indices']).reshape(-1, 3)
            original = accessor_array(tree, binary, attributes['NORMAL'])[faces].reshape(-1, 3)
            corners = original.reshape(-1, 3, 3)
            flat = np.linalg.norm(corners-corners[:, :1], axis=2).max(axis=1) <= 1e-6
            record.update(triangles=len(faces), originally_flat_triangles=int(flat.sum()))
            if not flat.all():
                edits.append({**record, 'status': 'retained', 'reason': 'authored smooth normals exist in this primitive'})
                continue
            triangles = vertices[faces].astype(np.float64)
            cross = np.cross(triangles[:, 1]-triangles[:, 0], triangles[:, 2]-triangles[:, 0])
            lengths = np.linalg.norm(cross, axis=1)
            normal_lengths = np.linalg.norm(original, axis=1)
            if np.any(lengths <= 1e-20) or np.any(normal_lengths <= 1e-12):
                edits.append({**record, 'status': 'retained', 'reason': 'degenerate surface or invalid authored normal'})
                continue
            face_normals = np.repeat(cross/lengths[:, None], 3, axis=0)
            normalized_original = original/normal_lengths[:, None]
            # Equal corner normals can be an intentional tilted normal field.
            # Accept only geometric flat normals, allowing float32 CAD rounding.
            if not np.isfinite(normalized_original).all():
                edits.append({**record, 'status': 'retained', 'reason': 'authored normals differ from geometric face directions'})
                continue
            direction_error = np.linalg.norm(normalized_original-face_normals, axis=1).reshape(-1, 3).max(axis=1)
            eligible = direction_error <= 2e-3
            protected = ~eligible
            record.update(eligible_faces=int(eligible.sum()), protected_faces=int(protected.sum()),
                          protected_area_fraction=float(lengths[protected].sum()/lengths.sum()),
                          surface_area_local=float(lengths.sum()/2), protected_surface_area_local=float(lengths[protected].sum()/2),
                          processing_policy='protect_direction_outlier_faces' if preserve_direction_outliers else 'whole_primitive_guard')
            if protected.any() and (not preserve_direction_outliers or not eligible.any()):
                edits.append({**record, 'status': 'retained', 'reason': 'authored normals differ from geometric face directions'})
                continue
            if protected.any():
                # Excluded faces contribute neither graph edges nor area weights.
                # Keep their authored float32 normal tuples, including signed zero.
                smooth = original.copy().reshape(-1, 3, 3)
                smooth[eligible] = smoother(vertices, faces[eligible], angle=angle).reshape(-1, 3, 3)
                smooth = smooth.reshape(-1, 3)
                protected_masks[(mi, pi)] = protected
            else:
                smooth = smoother(vertices, faces, angle=angle)
            # Keep original flat directions when there is no meaningful smoothing.
            equivalent = np.linalg.norm(smooth-original, axis=1) <= 1e-6
            smooth[equivalent] = original[equivalent]
            changed_corners = int((~equivalent).sum())
            if not changed_corners:
                edits.append({**record, 'status': 'retained', 'reason': 'no smooth fan at this threshold'})
                continue
            if protected.any() or selected is not None:
                tuples = np.ascontiguousarray(np.column_stack((vertices[faces].reshape(-1, 3), smooth)), dtype='<f4')
                opaque = tuples.view(np.dtype((np.void, tuples.dtype.itemsize*tuples.shape[1]))).reshape(-1)
                _, first, inverse = np.unique(opaque, return_index=True, return_inverse=True)
                complete = tuples[first]
                new_vertices, new_faces, new_normals = complete[:, :3], inverse.reshape(faces.shape), complete[:, 3:]
                if protected.any() and new_normals[new_faces][protected].tobytes() != corners[protected].tobytes():
                    raise ValueError('Protected authored corner normal bytes changed')
            else:
                new_vertices, new_faces, new_normals = compact(vertices, faces, smooth)
            if not np.array_equal(new_vertices[new_faces], vertices[faces]):
                raise ValueError('Normal refinement moved or reordered triangle corners')
            target = result['meshes'][mi]['primitives'][pi]
            target['attributes']['POSITION'] = append_accessor(new_vertices.astype('<f4'), 5126, 'VEC3', position=True)
            target['attributes']['NORMAL'] = append_accessor(new_normals.astype('<f4'), 5126, 'VEC3')
            component, dtype = (5123, '<u2') if int(new_faces.max()) < 65535 else (5125, '<u4')
            target['indices'] = append_accessor(new_faces.astype(dtype).reshape(-1), component, 'SCALAR')
            new_corners = new_normals[new_faces]
            flat_after = np.linalg.norm(new_corners-new_corners[:, :1], axis=2).max(axis=1) <= 1e-6
            source_normal_bytes = np.ascontiguousarray(original, dtype='<f4').view(np.dtype((np.void, 12))).reshape(-1)
            output_normal_bytes = np.ascontiguousarray(new_corners, dtype='<f4').view(np.dtype((np.void, 12))).reshape(-1)
            edits.append({**record, 'status': 'refined', 'changed_normal_corners': changed_corners,
                          'changed_normal_corners_exact': int((source_normal_bytes != output_normal_bytes).sum()),
                          'flat_triangles_after': int(flat_after.sum()), 'smooth_triangles_after': int((~flat_after).sum()),
                          'corner_positions_exact': True, 'protected_normal_corners_byte_exact': True})
    if not any(edit['status'] == 'refined' for edit in edits):
        return copy.deepcopy(tree), binary, edits
    target, target_binary = prune_accessors(result, bytes(packed))
    if target.get('materials') != tree.get('materials') or target.get('nodes') != tree.get('nodes') or target.get('scenes') != tree.get('scenes'):
        raise ValueError('Material, node or scene definitions changed')
    refined_keys = {(edit['mesh'], edit['primitive']) for edit in edits if edit['status'] == 'refined'}
    # Independently re-read all emitted triangle corners and every retained group.
    for mi, (old_mesh, new_mesh) in enumerate(zip(tree['meshes'], target['meshes'])):
        for pi, (old, new) in enumerate(zip(old_mesh['primitives'], new_mesh['primitives'])):
            oi = accessor_array(tree, binary, old['indices']).reshape(-1)
            ni = accessor_array(target, target_binary, new['indices']).reshape(-1)
            ov = accessor_array(tree, binary, old['attributes']['POSITION'])
            nv = accessor_array(target, target_binary, new['attributes']['POSITION'])
            if not np.array_equal(ov[oi], nv[ni]):
                raise ValueError('Emitted corner positions/winding differ')
            if (mi, pi) not in refined_keys:
                for semantic, old_accessor in old['attributes'].items():
                    before = accessor_array(tree, binary, old_accessor)[oi]
                    after = accessor_array(target, target_binary, new['attributes'][semantic])[ni]
                    if before.tobytes() != after.tobytes():
                        raise ValueError('Retained authored corner attributes changed')
            if (mi, pi) in protected_masks:
                old_normals = accessor_array(tree, binary, old['attributes']['NORMAL'])[oi].reshape(-1, 3, 3)
                new_normals = accessor_array(target, target_binary, new['attributes']['NORMAL'])[ni].reshape(-1, 3, 3)
                mask = protected_masks[(mi, pi)]
                if old_normals[mask].tobytes() != new_normals[mask].tobytes():
                    raise ValueError('Emitted protected authored normal bytes differ')
    return target, target_binary, edits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--files', nargs='+', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--angle', type=float, default=30)
    parser.add_argument('--preserve-direction-outliers', action='store_true',
                        help='Exclude direction-outlier faces from fans and preserve their authored normal bytes')
    parser.add_argument('--oriented-sheets', action='store_true',
                        help='Smooth oriented CAD front/back sheets without blending opposing faces')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if output.is_relative_to(root) or root.is_relative_to(output) or not 0 < args.angle < 90:
        parser.error('Use an external output root and a smoothing threshold between 0 and 90 degrees')
    smoother = None
    if args.oriented_sheets:
        from smooth_oriented_sheets import smooth_oriented_normals
        smoother = smooth_oriented_normals
    records = []
    for relative in args.files:
        source = (root/relative).resolve()
        if not source.is_relative_to(root) or 'Visual' not in source.parts or source.suffix != '.glb':
            parser.error('Inputs must be visual GLBs inside the source root')
        payload = source.read_bytes()
        tree, binary = read_glb(payload)
        target, target_binary, edits = refine(tree, binary, args.angle, smoother=smoother,
                                            preserve_direction_outliers=args.preserve_direction_outliers)
        candidate = encode_glb(target, target_binary)
        validate_glb(candidate)
        destination = output/source.relative_to(root)
        if destination.exists() and destination.read_bytes() != candidate:
            raise ValueError('Review output already differs: '+str(destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(candidate)
        records.append({'path': source.relative_to(root).as_posix(), 'source_sha256': hashlib.sha256(payload).hexdigest(),
                        'output_sha256': hashlib.sha256(candidate).hexdigest(), 'source_bytes': len(payload), 'output_bytes': len(candidate),
                        'materials_nodes_scenes_unchanged': True, 'triangle_corners_and_winding_exact': True, 'primitives': edits})
    summary = {'files': len(records), 'refined_primitives': sum(e['status']=='refined' for r in records for e in r['primitives']),
               'originally_flat_triangles_in_refined_groups': sum(e['triangles'] for r in records for e in r['primitives'] if e['status']=='refined'),
               'changed_normal_corners': sum(e.get('changed_normal_corners',0) for r in records for e in r['primitives']),
               'changed_normal_corners_exact': sum(e.get('changed_normal_corners_exact',0) for r in records for e in r['primitives']),
               'protected_faces': sum(e.get('protected_faces',0) for r in records for e in r['primitives']),
               'eligible_faces': sum(e.get('eligible_faces',0) for r in records for e in r['primitives']),
               'preserve_direction_outliers': args.preserve_direction_outliers,
               'normal_method': 'oriented_sheets' if args.oriented_sheets else 'manifold_fans',
               'source_bytes': sum(r['source_bytes'] for r in records), 'output_bytes': sum(r['output_bytes'] for r in records), 'angle_degrees': args.angle}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({'summary': summary, 'files': records},indent=2)+'\n')
    print(json.dumps(summary),flush=True)


if __name__ == '__main__':
    main()
