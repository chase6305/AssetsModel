"""Batch compatible flat-scene visual GLB primitives into a separate trial tree.

All triangle and line corner attributes, node transforms and material indices are
verified as exact multisets per scene. This never deletes opposite CAD surfaces.
The source assets are read-only; output must be outside the source asset tree.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

import numpy as np
from glb_format import read_glb, validate_glb
from analyze_render_geometry import values
from refine_materials import ROOT, encode_glb


class Unsupported(ValueError):
    pass


def matrix(node):
    if 'matrix' in node:
        return np.asarray(node['matrix'], dtype=np.float64).reshape(4, 4).T
    x, y, z, w = node.get('rotation', [0, 0, 0, 1])
    m = np.eye(4)
    m[:3, :3] = [[1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
                 [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
                 [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)]]
    m[:3, :3] = m[:3, :3] @ np.diag(node.get('scale', [1, 1, 1]))
    m[:3, 3] = node.get('translation', [0, 0, 0])
    return m


def check_supported(tree):
    for field in ('extensions', 'extensionsUsed', 'extensionsRequired', 'textures', 'images', 'skins', 'animations', 'cameras'):
        if tree.get(field):
            raise Unsupported(f'Unsupported root field: {field}')
    for material in tree.get('materials', []):
        if material.get('extensions') or material.get('alphaMode', 'OPAQUE') != 'OPAQUE':
            raise Unsupported('Transparent or extended materials require separate review')
        if any('Texture' in k for k in material) or any('Texture' in k for k in material.get('pbrMetallicRoughness', {})):
            raise Unsupported('Textured material')
    for node in tree.get('nodes', []):
        if any(k not in ('name', 'mesh', 'matrix', 'translation', 'rotation', 'scale', 'extras') for k in node):
            raise Unsupported('Only flat, unskinned mesh nodes are supported')
        if 'mesh' not in node:
            raise Unsupported('Non-mesh nodes need separate handling')
    for scene in tree.get('scenes', []):
        if len(scene.get('nodes', [])) != len(set(scene.get('nodes', []))):
            raise Unsupported('Repeated scene node reference')
    for field in ('accessors', 'bufferViews', 'buffers'):
        for item in tree.get(field, []):
            if item.get('extensions') or item.get('extras'):
                raise Unsupported(f'Metadata-bearing {field} require separate handling')
    for mesh in tree['meshes']:
        if mesh.get('extensions') or mesh.get('weights') or mesh.get('extras'):
            raise Unsupported('Extended, morphing or metadata-bearing mesh')
        for primitive in mesh['primitives']:
            if 'indices' not in primitive:
                raise Unsupported('Non-indexed primitive requires separate handling')
            if set(primitive) - {'attributes', 'indices', 'material', 'mode', 'extras'}:
                raise Unsupported('Extended or morphing primitive')
            if primitive.get('mode', 4) not in (1, 4):
                raise Unsupported('Only indexed lines and triangles are supported')
            for semantic, ai in primitive['attributes'].items():
                if semantic not in ('POSITION', 'NORMAL', 'TANGENT', 'TEXCOORD_0', 'TEXCOORD_1', 'COLOR_0'):
                    raise Unsupported(f'Unsupported vertex attribute: {semantic}')
                if tree['accessors'][ai].get('sparse'):
                    raise Unsupported('Sparse accessor')


def signature(tree, primitive):
    return tuple((semantic, tree['accessors'][ai]['componentType'], tree['accessors'][ai]['type'],
                  bool(tree['accessors'][ai].get('normalized', False))) for semantic, ai in sorted(primitive['attributes'].items()))


def check_opaque_overlap_order(tree, binary):
    """Reject exact multi-material overlaps whose visible result can depend on draw order.

    Explicit opposite-winding, single-sided CAD surfaces remain supported.
    """
    for scene in tree['scenes']:
        coincident = {}
        for ni in scene.get('nodes', []):
            node = tree['nodes'][ni]
            transform = matrix(node)
            for primitive in tree['meshes'][node['mesh']]['primitives']:
                if primitive.get('mode', 4) != 4:
                    continue
                positions = values(tree, binary, primitive['attributes']['POSITION']).astype(np.float64)
                positions = positions @ transform[:3, :3].T + transform[:3, 3]
                faces = values(tree, binary, primitive['indices']).ravel().reshape(-1, 3)
                material = primitive.get('material')
                double_sided = tree['materials'][material].get('doubleSided', False) if material is not None else False
                for face in faces:
                    corners = [tuple(v) for v in positions[face]]
                    key = tuple(sorted(corners))
                    oriented = min(tuple(corners[i:] + corners[:i]) for i in range(3))
                    existing = coincident.setdefault(key, [])
                    if any(other_material != material and (other_orientation == oriented or double_sided or other_double)
                           for other_orientation, other_material, other_double in existing):
                        raise Unsupported('Coincident multi-material surfaces can depend on draw order')
                    existing.append((oriented, material, double_sided))


def scene_corner_multisets(tree, binary):
    """Byte-exact local attributes plus independently computed world P/N."""
    results = []
    for scene in tree['scenes']:
        records = Counter()
        for ni in scene.get('nodes', []):
            node = tree['nodes'][ni]
            transform = matrix(node)
            normal_transform = np.linalg.inv(transform[:3, :3])
            for primitive in tree['meshes'][node['mesh']]['primitives']:
                width = 3 if primitive.get('mode', 4) == 4 else 2
                indices = values(tree, binary, primitive['indices']).ravel().reshape(-1, width)
                attrs = {semantic: values(tree, binary, ai) for semantic, ai in primitive['attributes'].items()}
                vertices = attrs['POSITION'].astype(np.float64)
                world = vertices @ transform[:3, :3].T + transform[:3, 3]
                world_normal = attrs['NORMAL'].astype(np.float64) @ normal_transform if 'NORMAL' in attrs else None
                header = (primitive.get('mode', 4), primitive.get('material'), signature(tree, primitive),
                          transform.tobytes(), json.dumps(primitive.get('extras'), sort_keys=True))
                for face in indices:
                    raw = b''.join(attrs[name][face].tobytes() for name in sorted(attrs))
                    raw += world[face].tobytes()
                    if world_normal is not None:
                        raw += world_normal[face].tobytes()
                    records[(header, raw)] += 1
        results.append(records)
    return results


def batch(tree, binary):
    check_supported(tree)
    check_opaque_overlap_order(tree, binary)
    groups = {}
    for ni, node in enumerate(tree['nodes']):
        membership = tuple(i for i, scene in enumerate(tree['scenes']) if ni in scene.get('nodes', []))
        if not membership:
            raise Unsupported('Unreferenced nodes need separate handling')
        node_payload = {k: v for k, v in node.items() if k not in ('name', 'mesh')}
        for pi, primitive in enumerate(tree['meshes'][node['mesh']]['primitives']):
            mode = primitive.get('mode', 4)
            key = (primitive.get('material'), signature(tree, primitive), matrix(node).tobytes(), membership,
                   json.dumps(node_payload, sort_keys=True), json.dumps(primitive.get('extras'), sort_keys=True),
                   (ni, pi) if mode != 4 else None)
            groups.setdefault(key, []).append((ni, pi, primitive))
    target = {k: copy.deepcopy(v) for k, v in tree.items() if k not in ('nodes', 'meshes', 'accessors', 'bufferViews', 'buffers')}
    target.update(nodes=[], meshes=[], accessors=[], bufferViews=[])
    for scene in target['scenes']:
        scene['nodes'] = []
    packed = bytearray()

    def accessor(array, component, kind, normalized=False, position=False, index=False):
        array = np.ascontiguousarray(array)
        packed.extend(b'\0' * (-len(packed) % 4))
        data = array.tobytes()
        byte_stride = None
        if not index:
            row_bytes = array.dtype.itemsize * array.shape[1]
            aligned_stride = (row_bytes + 3) // 4 * 4
            if aligned_stride != row_bytes:
                # glTF vertex rows must be four-byte aligned, including legal
                # normalized RGB byte colors and normalized byte UV pairs.
                padded = np.zeros((len(array), aligned_stride), dtype=np.uint8)
                padded[:, :row_bytes] = array.view(np.uint8).reshape(len(array), row_bytes)
                data, byte_stride = padded.tobytes(), aligned_stride
        view = len(target['bufferViews'])
        target['bufferViews'].append({'buffer': 0, 'byteOffset': len(packed), 'byteLength': len(data),
                                      'target': 34963 if index else 34962})
        if byte_stride is not None:
            target['bufferViews'][-1]['byteStride'] = byte_stride
        packed.extend(data)
        value = {'bufferView': view, 'componentType': component, 'type': kind, 'count': len(array)}
        if normalized:
            value['normalized'] = True
        if position:
            value.update(min=array.min(axis=0).tolist(), max=array.max(axis=0).tolist())
        ai = len(target['accessors'])
        target['accessors'].append(value)
        return ai

    for group_index, (key, sources) in enumerate(groups.items()):
        semantic_specs = key[1]
        arrays, local_indices, offset = [], [], 0
        for ni, pi, primitive in sources:
            fields = []
            for name, *_ in semantic_specs:
                attribute = np.ascontiguousarray(values(tree, binary, primitive['attributes'][name]))
                fields.append(attribute.view(np.uint8).reshape(len(attribute), -1))
            data = np.concatenate(fields, axis=1)
            arrays.append(data)
            local_indices.append(values(tree, binary, primitive['indices']).ravel().astype(np.int64) + offset)
            offset += len(data)
        all_values, indices = np.concatenate(arrays), np.concatenate(local_indices)
        used = all_values[indices]
        opaque = np.ascontiguousarray(used).view(np.dtype((np.void, used.shape[1]))).ravel()
        _, first, inverse = np.unique(opaque, return_index=True, return_inverse=True)
        compact = used[first]
        primitive = {k: copy.deepcopy(v) for k, v in sources[0][2].items() if k not in ('attributes', 'indices')}
        primitive['attributes'] = {}
        byte_offset = 0
        for name, component, kind, normalized in semantic_specs:
            old_values = values(tree, binary, sources[0][2]['attributes'][name])
            byte_width = old_values.dtype.itemsize * old_values.shape[1]
            field = np.ascontiguousarray(compact[:, byte_offset:byte_offset + byte_width]).view(old_values.dtype).reshape(-1, old_values.shape[1])
            primitive['attributes'][name] = accessor(field, component, kind, normalized, position=name == 'POSITION')
            byte_offset += byte_width
        component, dtype = (5123, '<u2') if inverse.max() < 65535 else (5125, '<u4')
        primitive['indices'] = accessor(inverse.astype(dtype).reshape(-1, 1), component, 'SCALAR', index=True)
        mesh_index = len(target['meshes'])
        target['meshes'].append({'name': f'batch-{group_index}', 'primitives': [primitive]})
        node = copy.deepcopy(tree['nodes'][sources[0][0]])
        node.update(name=f'batch-{group_index}', mesh=mesh_index)
        node_index = len(target['nodes'])
        target['nodes'].append(node)
        for scene_index in key[3]:
            target['scenes'][scene_index]['nodes'].append(node_index)
    packed.extend(b'\0' * (-len(packed) % 4))
    target['buffers'] = [{'byteLength': len(packed)}]
    return target, bytes(packed)


def draw_calls(tree):
    return sum(sum(p.get('mode', 4) == 4 for p in tree['meshes'][tree['nodes'][ni]['mesh']]['primitives'])
               for ni in tree['scenes'][tree.get('scene', 0)].get('nodes', []))


def load_benchmark(payload, repeats=5):
    import io
    import trimesh
    timings = []
    for _ in range(repeats):
        start = time.perf_counter()
        scene = trimesh.load_scene(io.BytesIO(payload), file_type='glb', process=False)
        timings.append(time.perf_counter() - start)
    return {'median_ms': float(np.median(timings) * 1000), 'runs_ms': [x * 1000 for x in timings], 'loaded_geometries': len(scene.geometry)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--files', nargs='+', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        parser.error('Trial output must not overlap source asset tree')
    records = []
    for rel in args.files:
        source = (root / rel).resolve()
        if not source.is_relative_to(root):
            parser.error('Source must be inside asset root')
        record = {'path': source.relative_to(root).as_posix()}
        old = source.read_bytes()
        original, original_bin = read_glb(old)
        try:
            target, binary = batch(original, original_bin)
            new = encode_glb(target, binary)
            parsed, parsed_bin = read_glb(new)
            validate_glb(new)
            before = scene_corner_multisets(original, original_bin)
            after = scene_corner_multisets(parsed, parsed_bin)
            if before != after:
                raise ValueError('Exact per-scene corner/material/world-transform multiset changed')
            if original.get('materials') != parsed.get('materials'):
                raise ValueError('Material definitions changed')
            target_path = output / source.relative_to(root)
            if target_path.exists() and target_path.read_bytes() != new:
                raise ValueError(f'Trial output already differs: {target_path}')
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(new)
            record.update(status='trial', source_sha256=hashlib.sha256(old).hexdigest(), trial_sha256=hashlib.sha256(new).hexdigest(),
                          before_bytes=len(old), after_bytes=len(new), before_draw_calls=draw_calls(original), after_draw_calls=draw_calls(parsed),
                          exact_corner_material_world_multisets_equal=True, scene_count=len(parsed['scenes']),
                          scene_primitive_counts=[sum(c.values()) for c in before], materials_unchanged=True,
                          before_load=load_benchmark(old), after_load=load_benchmark(new))
        except Unsupported as exc:
            record.update(status='skipped', reason=str(exc))
        records.append(record)
        print(json.dumps(record), flush=True)
    summary = {'files': len(records), 'trials': sum(r['status'] == 'trial' for r in records),
               'before_bytes': sum(r.get('before_bytes', 0) for r in records), 'after_bytes': sum(r.get('after_bytes', 0) for r in records),
               'before_draw_calls': sum(r.get('before_draw_calls', 0) for r in records), 'after_draw_calls': sum(r.get('after_draw_calls', 0) for r in records)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({'summary': summary, 'files': records}, indent=2) + '\n')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
