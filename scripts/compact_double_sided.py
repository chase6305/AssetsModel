"""Replace exact opaque front/back surface pairs with glTF double-sided faces.

Only same-primitive pairs with identical positions and exactly opposite stored
float normals qualify. UVs, transparency and other vertex attributes exclude a
primitive. Other faces retain their original material and culling. Collision
assets are excluded by the CLI. This preserves glTF raster shading; ray tracing
of formerly coincident surfaces can behave differently. Dry run by default.
"""

import copy
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from audit_asset_quality import inventory, scene_meshes
from batch_glb import array, batch, export
from glb_format import read_glb, validate_glb


def exact_pairs(tree, binary, primitive):
    if primitive.get('mode', 4) != 4 or set(primitive['attributes']) != {'POSITION', 'NORMAL'}:
        return []
    if 'material' not in primitive:
        return []
    normal_accessor = tree['accessors'][primitive['attributes']['NORMAL']]
    if normal_accessor['componentType'] != 5126 or normal_accessor['type'] != 'VEC3':
        return []
    material = tree['materials'][primitive['material']]
    if material.get('alphaMode', 'OPAQUE') != 'OPAQUE' or material.get('doubleSided', False):
        return []
    if 'extensions' in material or any('Texture' in key for key in material):
        return []
    pbr = material.get('pbrMetallicRoughness', {})
    if any('Texture' in key for key in pbr):
        return []
    positions = array(tree, binary, primitive['attributes']['POSITION'])
    normals = array(tree, binary, primitive['attributes']['NORMAL'])
    indices = array(tree, binary, primitive['indices']).ravel().reshape(-1, 3)
    _, welded = np.unique(positions, axis=0, return_inverse=True)
    faces = welded[indices]
    _, groups, counts = np.unique(np.sort(faces, axis=1), axis=0, return_inverse=True, return_counts=True)
    ordering = np.argsort(groups, kind='stable')
    starts = np.cumsum(counts) - counts
    parity = (faces[:, 0] > faces[:, 1]) ^ (faces[:, 0] > faces[:, 2]) ^ (faces[:, 1] > faces[:, 2])
    pairs = []
    for group in np.flatnonzero(counts == 2):
        i, j = ordering[starts[group]:starts[group]+2]
        if parity[i] == parity[j] or len(set(faces[i])) != 3:
            continue
        a, b = indices[i][np.argsort(faces[i])], indices[j][np.argsort(faces[j])]
        if np.array_equal(normals[a], -normals[b]):
            pairs.append((int(i), int(j)))
    return pairs


def semantic_signature(tree, binary):
    result = []
    for mesh in tree['meshes']:
        grouped = {}
        for primitive in mesh['primitives']:
            mode = primitive.get('mode', 4)
            material = tree['materials'][primitive['material']] if 'material' in primitive else {}
            effective = {k: v for k, v in material.items() if k not in {'name', 'extras', 'doubleSided'}}
            key = (mode, json.dumps(effective, sort_keys=True))
            indices = array(tree, binary, primitive['indices']).ravel().reshape(-1, 3 if mode == 4 else 2)
            streams = {key: array(tree, binary, accessor)[indices].copy() for key, accessor in sorted(primitive['attributes'].items())}
            variants = [streams]
            if mode == 4 and material.get('doubleSided', False):
                back = {k: v[:, [0, 2, 1]].copy() for k, v in streams.items()}
                if 'NORMAL' in back:
                    back['NORMAL'] *= -1
                variants.append(back)
            records = grouped.setdefault(key, [])
            for variant in variants:
                positions = variant['POSITION']
                start = np.lexsort((positions[:, :, 2], positions[:, :, 1], positions[:, :, 0]), axis=1)[:, 0]
                order = (start[:, None] + np.arange(indices.shape[1])[None, :]) % indices.shape[1]
                values = []
                for name, stream in variant.items():
                    stream = np.take_along_axis(stream, order[:, :, None], axis=1)
                    stream[stream == 0] = 0  # Signed zero has identical shading semantics.
                    values.append(stream.view(np.uint8).reshape(len(stream), -1))
                packed = np.concatenate(values, axis=1)
                records.extend(row.tobytes() for row in packed)
        result.append({key: hashlib.sha256(b''.join(sorted(records))).hexdigest() for key, records in grouped.items()})
    return result


def compact_pairs(payload, batch_first=True):
    original = payload
    tree, binary = read_glb(payload)
    validate_glb(payload)
    allowed = {'asset', 'scene', 'scenes', 'nodes', 'meshes', 'materials',
               'accessors', 'bufferViews', 'buffers', 'extras'}
    if set(tree) - allowed or any(set(node) - {'mesh', 'name'} for node in tree['nodes']):
        raise ValueError('Only static flat identity-node GLBs are supported')
    for mesh in tree['meshes']:
        if set(mesh) - {'name', 'primitives', 'extras'}:
            raise ValueError('Unsupported mesh metadata')
        for primitive in mesh['primitives']:
            if set(primitive) - {'mode', 'attributes', 'indices', 'material'}:
                raise ValueError('Morph targets and primitive extensions are unsupported')
    source_faces = validate_glb(payload)['faces']
    source_primitives = sum(len(x['primitives']) for x in tree['meshes'])
    source_draws = sum(sum(p.get('mode', 4) == 4 for p in tree['meshes'][mi]['primitives'])
                       for mi in scene_meshes(tree))
    # A temporary byte increase during batching can be outweighed by pairing.
    # batch() independently verifies every indexed corner and scene membership.
    if batch_first and all(len(mesh['primitives']) == 1 for mesh in tree['meshes']):
        payload, _ = batch(payload)
        tree, binary = read_glb(payload)
    result = copy.deepcopy(tree)
    result['accessors'] = []
    result['bufferViews'] = []
    packed = bytearray()
    material_map = {}
    removed = 0

    def store(original, values, target, component=None):
        values = np.ascontiguousarray(values)
        packed.extend(b'\0' * (-len(packed) % 4))
        view = len(result['bufferViews'])
        result['bufferViews'].append({'buffer': 0, 'byteOffset': len(packed), 'byteLength': values.nbytes, 'target': target})
        packed.extend(values.tobytes())
        accessor = copy.deepcopy(original)
        accessor.pop('byteOffset', None)
        accessor.update(bufferView=view, count=len(values))
        if component is not None:
            accessor['componentType'] = component
        if 'min' in accessor:
            accessor['min'] = values.min(axis=0).reshape(-1).tolist()
        if 'max' in accessor:
            accessor['max'] = values.max(axis=0).reshape(-1).tolist()
        result['accessors'].append(accessor)
        return len(result['accessors']) - 1

    for mi, mesh in enumerate(tree['meshes']):
        result['meshes'][mi]['primitives'] = []
        for primitive in mesh['primitives']:
            mode = primitive.get('mode', 4)
            width = 3 if mode == 4 else 2
            indices = array(tree, binary, primitive['indices']).ravel().reshape(-1, width)
            pairs = exact_pairs(tree, binary, primitive)
            segments = [(indices, primitive.get('material'))]
            if pairs:
                keep, discard = np.asarray(pairs).T
                mask = np.ones(len(indices), dtype=bool)
                mask[keep] = mask[discard] = False
                old_material = primitive['material']
                if old_material not in material_map:
                    new_material = copy.deepcopy(tree['materials'][old_material])
                    new_material['doubleSided'] = True
                    new_material['name'] = new_material.get('name', 'surface') + ' (paired double sided)'
                    material_map[old_material] = len(result['materials'])
                    result['materials'].append(new_material)
                segments = [(indices[mask], old_material), (indices[np.sort(keep)], material_map[old_material])]
                removed += len(pairs)
            for faces, material in segments:
                if not len(faces):
                    continue
                used, inverse = np.unique(faces.ravel(), return_inverse=True)
                item = {k: copy.deepcopy(v) for k, v in primitive.items() if k not in {'attributes', 'indices', 'material'}}
                item['attributes'] = {}
                for name, ai in primitive['attributes'].items():
                    values = array(tree, binary, ai)[used]
                    item['attributes'][name] = store(tree['accessors'][ai], values, 34962)
                component, dtype = (5121, 'u1') if int(inverse.max()) < 255 else (5123, '<u2') if int(inverse.max()) < 65535 else (5125, '<u4')
                item['indices'] = store(tree['accessors'][primitive['indices']], inverse.astype(dtype).reshape(-1, 1), 34963, component)
                if material is not None:
                    item['material'] = material
                result['meshes'][mi]['primitives'].append(item)
    if not removed:
        return original, {'removed_triangles': 0}
    output = export(result, packed)
    validate_glb(output)
    if semantic_signature(tree, binary) != semantic_signature(*read_glb(output)):
        raise ValueError('Effective front/back geometry and shading changed')
    return output, {'removed_triangles': removed, 'triangles_before': source_faces,
                    'triangles_after': validate_glb(output)['faces'],
                    'bytes_before': len(original), 'bytes_after': len(output),
                    'primitives_before': source_primitives,
                    'primitives_after': sum(len(x['primitives']) for x in result['meshes']),
                    'surface_draws_before': source_draws,
                    'surface_draws_after': sum(sum(p.get('mode', 4) == 4 for p in result['meshes'][mi]['primitives'])
                                               for mi in scene_meshes(result)),
                    'effective_front_back_corner_signatures_equal': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', type=Path, nargs='+')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.apply and not args.backup_dir:
        parser.error('--apply requires --backup-dir')
    if args.apply and args.backup_dir.resolve().is_relative_to(root):
        parser.error('Keep backups outside the asset root')
    usage, _ = inventory(root)
    files = set()
    for path in args.paths:
        path = path if path.is_absolute() else root / path
        files.update(path.rglob('*.glb') if path.is_dir() else [path])
    rows = []
    for path in sorted(files):
        relative = path.resolve().relative_to(root)
        row = {'path': relative.as_posix()}
        try:
            if usage.get(relative.as_posix()) != {'visual'}:
                row.update(status='skipped', reason='Only URDF visual meshes unshared with collision are eligible')
                rows.append(row)
                continue
            payload = path.read_bytes()
            output, stats = compact_pairs(payload)
            row.update(stats, sha256_before=hashlib.sha256(payload).hexdigest(),
                       sha256_after=hashlib.sha256(output).hexdigest())
            eligible = stats['removed_triangles'] > 0 and len(output) < len(payload)
            row['status'] = 'eligible' if eligible else 'unchanged'
            if eligible and args.apply:
                backup = (args.backup_dir / relative).resolve()
                if backup.is_relative_to(root):
                    raise ValueError('Backup resolves inside the asset root')
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists() and backup.read_bytes() != payload:
                    raise ValueError('Existing backup differs; choose a fresh backup directory')
                if not backup.exists():
                    with backup.open('xb') as stream:
                        stream.write(payload)
                staging = path.with_suffix('.glb.tmp')
                staging.write_bytes(output)
                staging.replace(path)
                row['status'] = 'compacted'
        except (ValueError, OSError) as exc:
            row.update(status='error', error=str(exc))
        rows.append(row)
    summary = {'files': len(rows), 'applied': args.apply,
               'errors': sum(x['status'] == 'error' for x in rows),
               'eligible_files': sum(x['status'] in {'eligible', 'compacted'} for x in rows),
               'removed_triangles': sum(x.get('removed_triangles', 0) for x in rows
                                        if x['status'] in {'eligible', 'compacted'}),
               'bytes_saved': sum(x.get('bytes_before', 0) - x.get('bytes_after', 0) for x in rows
                                  if x['status'] in {'eligible', 'compacted'})}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({'summary': summary, 'files': rows}, indent=2, allow_nan=False) + '\n')
    print(json.dumps(summary))
    return int(summary['errors'] > 0 or not rows)


if __name__ == '__main__':
    raise SystemExit(main())
