"""Batch opaque static GLB surfaces by material without changing corner attributes.

Only the repository's flat, identity-node asset profile is supported. Every
scene is checked byte-for-byte at indexed corners before a result is returned.
The default is a dry run; --apply requires an external backup directory.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import struct
from pathlib import Path

import numpy as np

from glb_format import accessor_bytes, read_glb, validate_glb

DTYPES = {5120: '<i1', 5121: '<u1', 5122: '<i2', 5123: '<u2',
          5125: '<u4', 5126: '<f4'}


def array(tree, binary, index):
    raw, _, width = accessor_bytes(tree, binary, index)
    accessor = tree['accessors'][index]
    return np.frombuffer(raw, dtype=DTYPES[accessor['componentType']]).reshape(-1, width)


def layout(tree, primitive):
    return tuple((name, tree['accessors'][index]['componentType'],
                  tree['accessors'][index]['type'],
                  tree['accessors'][index].get('normalized', False))
                 for name, index in sorted(primitive['attributes'].items()))


def profile(tree):
    # Unknown data may refer to node/accessor indices. Refuse it instead of
    # silently discarding it or changing its meaning while rebuilding arrays.
    allowed = {'asset', 'scene', 'scenes', 'nodes', 'meshes', 'materials',
               'accessors', 'bufferViews', 'buffers', 'extras'}
    if set(tree) - allowed:
        raise ValueError('Unsupported GLB profile: animation, textures or extensions')
    for node in tree['nodes']:
        if set(node) - {'mesh', 'name'} or 'mesh' not in node:
            raise ValueError('Only flat identity mesh nodes can be batched')
    for mesh in tree['meshes']:
        if set(mesh) - {'primitives', 'name', 'extras'} or len(mesh['primitives']) != 1:
            raise ValueError('Expected one primitive per named mesh')
        if set(mesh.get('extras', {})) - {'sourceParts'}:
            raise ValueError('Unknown mesh metadata cannot be batched')
        primitive = mesh['primitives'][0]
        if set(primitive) - {'attributes', 'indices', 'mode', 'material'}:
            raise ValueError('Primitive metadata or morph targets cannot be batched')
    for scene in tree['scenes']:
        if set(scene) - {'nodes', 'name', 'extras'}:
            raise ValueError('Unsupported scene metadata')
        if len(set(scene['nodes'])) != len(scene['nodes']):
            raise ValueError('Repeated scene node')


def corner_signatures(tree, binary):
    """Per-scene/material/layout digests, including winding and corner normals."""
    result = []
    for scene in tree['scenes']:
        signatures = {}
        for node in scene['nodes']:
            primitive = tree['meshes'][tree['nodes'][node]['mesh']]['primitives'][0]
            key = (primitive.get('mode', 4), primitive.get('material'), layout(tree, primitive))
            digest, count = signatures.get(key, (hashlib.sha256(), 0))
            indices = array(tree, binary, primitive['indices']).ravel()
            # Interleaving each corner's complete attributes makes the digest
            # independent of how primitives split the same material stream.
            values = [array(tree, binary, primitive['attributes'][name])[indices]
                      .copy().view(np.uint8).reshape(len(indices), -1)
                      for name, *_ in key[2]]
            digest.update(np.concatenate(values, axis=1).tobytes())
            signatures[key] = digest, count + len(indices)
        result.append({key: (digest.hexdigest(), count)
                       for key, (digest, count) in signatures.items()})
    return result


def export(tree, binary):
    tree['buffers'] = [{'byteLength': len(binary)}]
    raw = json.dumps(tree, separators=(',', ':'), allow_nan=False).encode()
    raw += b' ' * (-len(raw) % 4)
    binary += b'\0' * (-len(binary) % 4)
    return (struct.pack('<III', 0x46546C67, 2, 28 + len(raw) + len(binary))
            + struct.pack('<II', len(raw), 0x4E4F534A) + raw
            + struct.pack('<II', len(binary), 0x004E4942) + binary)


def batch(payload):
    tree, binary = read_glb(payload)
    validate_glb(payload)
    profile(tree)
    groups = {}
    for index, node in enumerate(tree['nodes']):
        primitive = tree['meshes'][node['mesh']]['primitives'][0]
        material = primitive.get('material')
        blend = material is not None and tree['materials'][material].get('alphaMode') == 'BLEND'
        membership = tuple(i for i, scene in enumerate(tree['scenes']) if index in scene['nodes'])
        key = (primitive.get('mode', 4), material, layout(tree, primitive), membership,
               index if blend or primitive.get('mode', 4) != 4 else None)
        groups.setdefault(key, []).append(index)
    before = len(tree['nodes'])
    stats = {'primitives_before': before, 'primitives_after': len(groups),
             'vertices_before': 0, 'vertices_after': 0,
             'bytes_before': len(payload), 'bytes_after': len(payload)}
    if before == len(groups):
        return payload, stats
    result = {key: copy.deepcopy(value) for key, value in tree.items()
              if key not in {'nodes', 'meshes', 'accessors', 'bufferViews', 'buffers'}}
    for key in ('nodes', 'meshes', 'accessors', 'bufferViews'):
        result[key] = []
    packed = bytearray()

    def add_accessor(values, component, kind, target, normalized=False):
        packed.extend(b'\0' * (-len(packed) % 4))
        view = len(result['bufferViews'])
        result['bufferViews'].append({'buffer': 0, 'byteOffset': len(packed),
                                     'byteLength': values.nbytes, 'target': target})
        packed.extend(values.tobytes())
        accessor = {'bufferView': view, 'componentType': component,
                    'count': len(values), 'type': kind}
        if normalized:
            accessor['normalized'] = True
        if kind == 'VEC3' and component == 5126:
            accessor.update(min=values.min(axis=0).tolist(), max=values.max(axis=0).tolist())
        result['accessors'].append(accessor)
        return len(result['accessors']) - 1

    old_to_new = {}
    for group_index, (key, nodes) in enumerate(groups.items()):
        mode, material, attributes, _, _ = key
        streams = {name: [] for name, *_ in attributes}
        indices, vertex_offset = [], 0
        for node_index in nodes:
            primitive = tree['meshes'][tree['nodes'][node_index]['mesh']]['primitives'][0]
            for name, *_ in attributes:
                streams[name].append(array(tree, binary, primitive['attributes'][name]))
            indices.append(array(tree, binary, primitive['indices']).ravel().astype(np.int64) + vertex_offset)
            vertex_offset += len(streams['POSITION'][-1])
            old_to_new[node_index] = group_index
        indices = np.concatenate(indices)
        values = {name: np.concatenate(items) for name, items in streams.items()}
        complete = np.concatenate([values[name].view(np.uint8).reshape(vertex_offset, -1)
                                   for name, *_ in attributes], axis=1)
        # Exact byte equality preserves signed zero, UV seams and hard normals.
        _, first, inverse = np.unique(complete, axis=0, return_index=True, return_inverse=True)
        indices = inverse[indices]
        stats['vertices_before'] += vertex_offset
        stats['vertices_after'] += len(first)
        primitive = {'attributes': {}, 'mode': mode}
        for name, component, kind, normalized in attributes:
            primitive['attributes'][name] = add_accessor(values[name][first], component, kind, 34962, normalized)
        maximum = int(indices.max())
        component = 5121 if maximum < 255 else 5123 if maximum < 65535 else 5125
        primitive['indices'] = add_accessor(indices.astype(DTYPES[component]), component, 'SCALAR', 34963)
        if material is not None:
            primitive['material'] = material
        names = []
        for i in nodes:
            source_mesh = tree['meshes'][tree['nodes'][i]['mesh']]
            names.extend(source_mesh.get('extras', {}).get('sourceParts',
                         [tree['nodes'][i].get('name', f'node-{i}')]))
        name = tree['nodes'][nodes[0]].get('name', names[0]) if len(nodes) == 1 else f'batched-{group_index}-material-{material}'
        # Keep the original CAD part names available for inspection.
        mesh = {'name': name, 'primitives': [primitive]}
        if len(nodes) > 1 or 'extras' in tree['meshes'][tree['nodes'][nodes[0]]['mesh']]:
            mesh['extras'] = {'sourceParts': names}
        result['meshes'].append(mesh)
        result['nodes'].append({'name': name, 'mesh': group_index})
    for scene in result['scenes']:
        scene['nodes'] = list(dict.fromkeys(old_to_new[i] for i in scene['nodes']))
    output = export(result, packed)
    validate_glb(output)
    if corner_signatures(tree, binary) != corner_signatures(*read_glb(output)):
        raise ValueError('Batching changed scene membership or indexed corner attributes')
    stats['bytes_after'] = len(output)
    return output, stats


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
        parser.error('Backup directory must be outside the asset root')
    files = set()
    for path in args.paths:
        path = path if path.is_absolute() else root / path
        files.update(path.rglob('*.glb') if path.is_dir() else [path])
    rows = []
    for path in sorted(files):
        relative = path.resolve().relative_to(root)
        payload = path.read_bytes()
        row = {'path': relative.as_posix(), 'sha256_before': hashlib.sha256(payload).hexdigest()}
        try:
            output, stats = batch(payload)
            row.update(stats)
            eligible = stats['primitives_after'] < stats['primitives_before'] and len(output) < len(payload)
            row['status'] = 'batched' if eligible else 'unchanged'
            if eligible:
                row['sha256_after'] = hashlib.sha256(output).hexdigest()
                if args.apply:
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
        except ValueError as exc:
            row.update(status='skipped', reason=str(exc))
        rows.append(row)
        if row['status'] != 'unchanged':
            print(json.dumps(row), flush=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({'files': rows}, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
