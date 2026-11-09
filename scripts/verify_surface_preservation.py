"""Independently audit baseline asset surfaces with the Python standard library.

Material values and NORMAL may change. Every scene retains its oriented local
POSITION/other-attribute corner multiset and effective instance transforms.
No production converter, batcher, preview, numpy or trimesh reader is used.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct
from urllib.parse import unquote_to_bytes

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {5120: ('b', 1), 5121: ('B', 1), 5122: ('h', 2),
              5123: ('H', 2), 5125: ('I', 4), 5126: ('f', 4)}
WIDTHS = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}
IDENTITY = tuple(float(i == j) for i in range(4) for j in range(4))
FRAME_TOLERANCE = 1e-12


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def read_container(payload):
    if len(payload) < 28 or struct.unpack_from('<III', payload) != (0x46546C67, 2, len(payload)):
        raise ValueError('Invalid GLB header')
    chunks = []; offset = 12
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise ValueError('Truncated GLB chunk header')
        length, kind = struct.unpack_from('<II', payload, offset); offset += 8
        if length % 4 or offset + length > len(payload):
            raise ValueError('Invalid GLB chunk length')
        chunks.append((kind, payload[offset:offset + length])); offset += length
    if [kind for kind, _ in chunks] != [0x4E4F534A, 0x004E4942]:
        raise ValueError('Expected one JSON and one embedded BIN chunk')
    tree = json.loads(chunks[0][1]); binary = chunks[1][1]
    if tree.get('asset', {}).get('version') != '2.0':
        raise ValueError('Not glTF 2.0')
    buffers = tree.get('buffers', [])
    if len(buffers) != 1 or 'uri' in buffers[0] or not 0 <= len(binary) - buffers[0]['byteLength'] <= 3:
        raise ValueError('Expected one valid embedded buffer')
    if any(tree.get(key) for key in ('skins', 'animations', 'extensionsRequired', 'extensionsUsed', 'extensions')):
        raise ValueError('Animated or extension-bearing geometry needs a separate audit')
    return tree, binary


def view_bytes(tree, binary, index):
    view = tree['bufferViews'][index]
    offset = view.get('byteOffset', 0); length = view['byteLength']
    if view.get('buffer', 0) != 0 or offset < 0 or length < 0 or offset + length > tree['buffers'][0]['byteLength']:
        raise ValueError('Buffer view outside embedded buffer')
    return binary[offset:offset + length]


def read_accessor(tree, binary, index):
    item = tree['accessors'][index]
    if 'sparse' in item or item.get('extensions'):
        raise ValueError('Sparse/extended accessor needs a separate audit')
    code, size = COMPONENTS[item['componentType']]; width = WIDTHS[item['type']]
    packed = width * size; count = item['count']; offset = item.get('byteOffset', 0)
    view = tree['bufferViews'][item['bufferView']]; data = view_bytes(tree, binary, item['bufferView'])
    stride = view.get('byteStride', packed)
    if count < 0 or offset < 0 or stride < packed or stride % size or offset % size:
        raise ValueError('Invalid accessor layout')
    if count and offset + (count - 1) * stride + packed > len(data):
        raise ValueError('Accessor exceeds buffer view')
    rows = tuple(data[offset + i * stride:offset + i * stride + packed] for i in range(count))
    return rows, (item['componentType'], item['type'], bool(item.get('normalized', False))), code, width


def multiply(left, right):
    return tuple(sum(left[4*r+k] * right[4*k+c] for k in range(4)) for r in range(4) for c in range(4))


def matrix(node):
    if node.get('extensions') or 'skin' in node or 'weights' in node:
        raise ValueError('Extended/skinned/morphed node needs a separate audit')
    if 'matrix' in node:
        if any(key in node for key in ('translation', 'rotation', 'scale')):
            raise ValueError('Node contains both matrix and TRS')
        values = node['matrix']
        if len(values) != 16:
            raise ValueError('Invalid node matrix')
        result = tuple(float(values[c*4+r]) for r in range(4) for c in range(4))
    else:
        x, y, z, w = (float(v) for v in node.get('rotation', [0, 0, 0, 1]))
        length = math.sqrt(x*x+y*y+z*z+w*w)
        if not math.isfinite(length) or length <= 0:
            raise ValueError('Invalid node quaternion')
        x, y, z, w = x/length, y/length, z/length, w/length
        sx, sy, sz = (float(v) for v in node.get('scale', [1, 1, 1]))
        tx, ty, tz = (float(v) for v in node.get('translation', [0, 0, 0]))
        result = ((1-2*(y*y+z*z))*sx, 2*(x*y-z*w)*sy, 2*(x*z+y*w)*sz, tx,
                  2*(x*y+z*w)*sx, (1-2*(x*x+z*z))*sy, 2*(y*z-x*w)*sz, ty,
                  2*(x*z-y*w)*sx, 2*(y*z+x*w)*sy, (1-2*(x*x+y*y))*sz, tz,
                  0., 0., 0., 1.)
    if not all(math.isfinite(v) for v in result):
        raise ValueError('Non-finite node transform')
    return result


def texture_bindings(material):
    found = []
    def visit(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower().endswith('texture'):
                    found.append((path + (key,), canonical_json(child)))
                else:
                    visit(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + (str(index),))
    visit(material, ())
    return tuple(sorted(found))


def texture_resources(tree, binary):
    images = []
    for image in tree.get('images', []):
        item = dict(image)
        if 'bufferView' in item:
            item['embedded_content_sha256'] = digest(view_bytes(tree, binary, item.pop('bufferView')))
        elif item.get('uri', '').startswith('data:'):
            header, content = item.pop('uri').split(',', 1)
            item['data_uri_header'] = header
            item['embedded_content_sha256'] = digest(base64.b64decode(content) if header.endswith(';base64') else unquote_to_bytes(content))
        # External URIs must stay identical. Their old asset files are also
        # checked byte-for-byte by the repository-level audit.
        images.append(item)
    return canonical_json({'images': images, 'textures': tree.get('textures', []), 'samplers': tree.get('samplers', [])})


def describe_record(record):
    mode, frame, schema, bindings, corners = record
    position_index = next(i for i, item in enumerate(schema) if item[0] == 'POSITION')
    positions = []
    for corner in corners:
        raw = corner[position_index]
        positions.append(list(struct.unpack('<fff', raw)))
    return {'mode': mode, 'transform_bucket': frame, 'attribute_schema': schema,
            'positions': positions, 'texture_bindings': bindings,
            'corner_attribute_bytes_hex': [[raw.hex() for raw in corner] for corner in corners]}


def collect_scenes(tree, binary, source_frames=None):
    frames = [] if source_frames is None else source_frames
    counters = []; stats = []; max_error = 0.; accessor_cache = {}; primitive_cache = {}
    def frame_index(value):
        nonlocal max_error
        try:
            return frames.index(value)
        except ValueError:
            if source_frames is None:
                frames.append(value); return len(frames) - 1
            errors = [max(abs(a-b) for a, b in zip(value, candidate)) for candidate in frames]
            if not errors or min(errors) > FRAME_TOLERANCE:
                raise AssertionError('Effective geometry instance transform changed')
            smallest = min(errors); max_error = max(max_error, smallest)
            return errors.index(smallest)
    def attribute(index):
        if index not in accessor_cache:
            accessor_cache[index] = read_accessor(tree, binary, index)
        return accessor_cache[index]
    def primitive_records(mesh_index, primitive_index):
        key = (mesh_index, primitive_index)
        if key in primitive_cache:
            return primitive_cache[key]
        mesh = tree['meshes'][mesh_index]; primitive = mesh['primitives'][primitive_index]
        if mesh.get('extensions') or 'weights' in mesh or primitive.get('extensions') or 'targets' in primitive:
            raise ValueError('Extended/morphed primitive needs a separate audit')
        mode = primitive.get('mode', 4)
        if mode not in (1, 4):
            raise ValueError(f'Unsupported actual primitive mode {mode}')
        names = sorted(name for name in primitive['attributes'] if name != 'NORMAL')
        if 'POSITION' not in names:
            raise ValueError('Primitive has no POSITION')
        values = [attribute(primitive['attributes'][name]) for name in names]
        counts = {len(value[0]) for value in values}
        if len(counts) != 1:
            raise ValueError('Attribute row counts differ')
        count = counts.pop()
        position = values[names.index('POSITION')]
        if position[1] != (5126, 'VEC3', False):
            raise ValueError('Expected float32 XYZ POSITION')
        if any(not math.isfinite(v) for raw in position[0] for v in struct.unpack('<fff', raw)):
            raise ValueError('Non-finite POSITION')
        if 'indices' in primitive:
            rows, metadata, code, width = attribute(primitive['indices'])
            if metadata[0] not in (5121, 5123, 5125) or width != 1 or metadata[2]:
                raise ValueError('Invalid index accessor')
            indices = [struct.unpack('<' + code, row)[0] for row in rows]
        else:
            indices = list(range(count))
        arity = 3 if mode == 4 else 2
        if len(indices) % arity or any(index >= count for index in indices):
            raise ValueError('Invalid primitive index count/range')
        schema = tuple((name, *value[1]) for name, value in zip(names, values))
        material = tree.get('materials', [])[primitive['material']] if 'material' in primitive else {}
        if material.get('extensions'):
            raise ValueError('Material extensions require extension-aware surface validation')
        bindings = texture_bindings(material)
        records = []
        for start in range(0, len(indices), arity):
            corners = tuple(tuple(value[0][index] for value in values) for index in indices[start:start+arity])
            if mode == 4:
                corners = min(corners, corners[1:] + corners[:1], corners[2:] + corners[:2])
            records.append((mode, schema, bindings, corners))
        result = (records, len(indices), tuple(names))
        primitive_cache[key] = result
        return result
    nodes = tree['nodes']
    for scene in tree['scenes']:
        records = Counter(); instances = triangles = lines = corners = 0; semantics = set()
        stack = [(index, IDENTITY, frozenset()) for index in reversed(scene.get('nodes', []))]
        while stack:
            index, parent, ancestors = stack.pop()
            if index in ancestors:
                raise ValueError('Cyclic scene nodes')
            node = nodes[index]; effective = multiply(parent, matrix(node))
            stack.extend((child, effective, ancestors | {index}) for child in reversed(node.get('children', [])))
            if 'mesh' not in node:
                continue
            frame = frame_index(effective); instances += 1
            for pi in range(len(tree['meshes'][node['mesh']]['primitives'])):
                group, count, names = primitive_records(node['mesh'], pi)
                semantics.update(names); corners += count
                for mode, schema, bindings, vertex_rows in group:
                    records[mode, frame, schema, bindings, vertex_rows] += 1
                    triangles += mode == 4; lines += mode == 1
        counters.append(records)
        stats.append({'triangles': triangles, 'line_segments': lines, 'position_corners': corners,
                      'geometry_node_instances': instances, 'non_normal_attributes': sorted(semantics),
                      'distinct_oriented_records': len(records)})
    return counters, frames, stats, max_error


def verify_surfaces(before_bytes, after_bytes):
    before, before_bin = read_container(before_bytes); after, after_bin = read_container(after_bytes)
    if before.get('scene', 0) != after.get('scene', 0):
        raise AssertionError('Default scene selection changed')
    metadata = lambda tree: [{key: value for key, value in scene.items() if key != 'nodes'} for scene in tree['scenes']]
    if metadata(before) != metadata(after):
        raise AssertionError('Scene count/name/metadata changed')
    if texture_resources(before, before_bin) != texture_resources(after, after_bin):
        raise AssertionError('Texture image/sampler resources changed')
    old, frames, old_stats, _ = collect_scenes(before, before_bin)
    if before_bytes == after_bytes:
        new, new_stats, error = old, old_stats, 0.
    else:
        new, _, new_stats, error = collect_scenes(after, after_bin, frames)
    for index, (source, target) in enumerate(zip(old, new)):
        if source != target:
            missing = source - target; extra = target - source
            evidence = {'scene': index, 'missing_records': sum(missing.values()), 'extra_records': sum(extra.values())}
            if missing:
                evidence['first_missing'] = describe_record(next(iter(missing)))
            if extra:
                evidence['first_extra'] = describe_record(next(iter(extra)))
            raise AssertionError('Oriented scene surface multiset changed: ' + canonical_json(evidence))
    return {'scenes': len(old), 'all_scene_triangles': sum(item['triangles'] for item in old_stats),
            'all_scene_line_segments': sum(item['line_segments'] for item in old_stats),
            'all_scene_position_corners': sum(item['position_corners'] for item in old_stats),
            'distinct_effective_transforms': len(frames), 'max_effective_transform_error': error,
            'scene_details': [{'scene': index, 'source': source, 'current': target} for index, (source, target) in enumerate(zip(old_stats, new_stats))],
            'all_scene_oriented_position_and_non_normal_corner_bytes_exact': True,
            'texture_resources_and_bindings_preserved': True}


def mutable_visual(path):
    return path.parts[0] == 'RobotModel' and 'Visual' in path.parts and path.suffix.lower() == '.glb'


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, indent=2) + '\n'); temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=Path('/tmp/assetsmodel-20261103-baseline'))
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--json', type=Path, default=ROOT/'docs/quality/existing-surface-preservation.json')
    parser.add_argument('--resume', action='store_true', help='Reuse passing rows only when baseline/current/checker SHA-256 match')
    args = parser.parse_args(); baseline = args.baseline.resolve(); root = args.root.resolve()
    started = timestamp(); checker_sha = digest(Path(__file__).read_bytes())
    baseline_files = sorted(p.relative_to(baseline) for p in baseline.rglob('*') if p.is_file())
    current_files = sorted(p.relative_to(root) for category in ('RobotModel', 'CameraModel', 'ToolModel') for p in (root/category).rglob('*') if p.is_file())
    source_sha = {p.as_posix(): digest((baseline/p).read_bytes()) for p in baseline_files}
    current_sha = {p.as_posix(): digest((root/p).read_bytes()) for p in current_files}
    cache = {}
    if args.resume and args.json.is_file():
        prior = json.loads(args.json.read_text())
        if prior.get('checker_sha256') == checker_sha:
            cache = {row['path']: row for row in prior.get('files', []) if row.get('status') == 'passed'}
    report = {'status': 'in_progress', 'started_at': started, 'snapshot_completed_at': timestamp(),
              'baseline_root': str(baseline), 'current_root': str(root), 'checker_sha256': checker_sha,
              'input_manifest_sha256': digest(canonical_json({'baseline': source_sha, 'current': current_sha}).encode()),
              'method': 'Independent stdlib GLB/strided accessor decoder, every scene tree traversed with composed matrix/TRS; Counter of oriented local raw corner tuples, non-N attribute representation, texture binding and effective instance transform. Triangle cyclic origins allowed; winding and duplicate multiplicity preserved.',
              'intentional_exceptions': 'NORMAL and material scalar/color values may change; node/primitive counts and buffer/index layout may change. Added assets are inventoried without a baseline equivalence claim.',
              'scope_note': 'This is a geometry/non-N-attribute/texture-resource audit. It does not establish preservation of material assignment, alpha/double-sided visibility, lighting, or rendered appearance; those need separate material and visual review.',
              'transform_absolute_tolerance': FRAME_TOLERANCE,
              'protected_byte_rule': 'Every baseline file except RobotModel Visual GLB must remain byte identical, including all old URDF/STL/Collision/Camera/Tool and baseline user-dirty files.',
              'added_files': [{'path': p, 'current_sha256': current_sha[p]} for p in sorted(current_sha.keys()-source_sha.keys())],
              'files': [], 'summary': {}}
    for relative in baseline_files:
        name = relative.as_posix(); old_sha = source_sha[name]; new_sha = current_sha.get(name)
        cached = cache.get(name)
        if cached and cached['baseline_sha256'] == old_sha and cached.get('current_sha256') == new_sha:
            row = cached
        else:
            row = {'path': name, 'baseline_sha256': old_sha, 'current_sha256': new_sha,
                   'byte_identical': old_sha == new_sha, 'byte_identity_required': not mutable_visual(relative)}
            try:
                if new_sha is None:
                    raise AssertionError('Baseline file missing from current assets')
                if row['byte_identity_required'] and not row['byte_identical']:
                    raise AssertionError('Protected baseline file bytes changed')
                if relative.suffix.lower() == '.glb':
                    row['surface_verification'] = verify_surfaces((baseline/relative).read_bytes(), (root/relative).read_bytes())
                row['status'] = 'passed'
            except Exception as exc:
                row.update(status='failed', error=str(exc)); print('FAIL', name, exc, flush=True)
        report['files'].append(row)
        if len(report['files']) % 50 == 0:
            save_report(args.json, report); print('Checked', len(report['files']), '/', len(baseline_files), flush=True)
    changed_baseline = [p for p, value in source_sha.items() if not (baseline/p).is_file() or digest((baseline/p).read_bytes()) != value]
    changed_current = [p for p, value in current_sha.items() if not (root/p).is_file() or digest((root/p).read_bytes()) != value]
    final_current_files = {p.relative_to(root).as_posix() for category in ('RobotModel', 'CameraModel', 'ToolModel') for p in (root/category).rglob('*') if p.is_file()}
    changed_inventory = sorted(final_current_files ^ current_sha.keys())
    rows = report['files']; glbs = [row for row in rows if row['path'].lower().endswith('.glb')]
    passed_glbs = [row for row in glbs if row['status'] == 'passed']
    byte_groups = {}
    for label, predicate in {
        'old_urdf': lambda p: p.suffix.lower() == '.urdf',
        'old_stl': lambda p: p.suffix.lower() == '.stl',
        'collision_files': lambda p: 'Collision' in p.parts,
        'camera_files': lambda p: p.parts[0] == 'CameraModel',
        'tool_files_including_baseline_user_changes': lambda p: p.parts[0] == 'ToolModel',
    }.items():
        group = [row for row in rows if predicate(Path(row['path']))]
        byte_groups[label] = {'files': len(group), 'byte_identical': sum(row['byte_identical'] for row in group)}
    report['summary'] = {'baseline_files': len(rows), 'baseline_glbs': len(glbs), 'baseline_urdfs': sum(p.suffix.lower() == '.urdf' for p in baseline_files),
                         'baseline_stls': sum(p.suffix.lower() == '.stl' for p in baseline_files),
                         'baseline_byte_identical_files': sum(row['byte_identical'] for row in rows),
                         'existing_glbs_surface_verified': len(passed_glbs),
                         'byte_identity_by_group_overlapping': byte_groups,
                         'protected_files_byte_exact': sum(row['byte_identity_required'] and row['byte_identical'] for row in rows),
                         'changed_robot_visual_glbs': sum(not row['byte_identical'] for row in glbs),
                         'added_files': len(report['added_files']), 'added_glbs': sum(row['path'].lower().endswith('.glb') for row in report['added_files']),
                         'added_urdfs': sum(row['path'].lower().endswith('.urdf') for row in report['added_files']),
                         'all_scene_triangles': sum(row['surface_verification']['all_scene_triangles'] for row in passed_glbs),
                         'all_scene_line_segments': sum(row['surface_verification']['all_scene_line_segments'] for row in passed_glbs),
                         'all_scene_position_corners_byte_exact': sum(row['surface_verification']['all_scene_position_corners'] for row in passed_glbs),
                         'max_effective_transform_error': max((row['surface_verification']['max_effective_transform_error'] for row in passed_glbs), default=0),
                         'failed_files': sum(row['status'] != 'passed' for row in rows),
                         'baseline_changed_during_audit': changed_baseline, 'current_changed_during_audit': changed_current,
                         'current_inventory_changed_during_audit': changed_inventory}
    report['summary']['errors'] = report['summary']['failed_files'] + len(changed_baseline) + len(changed_current) + len(changed_inventory)
    report['status'] = 'complete' if not report['summary']['errors'] else 'failed'
    report['completed_at'] = timestamp(); save_report(args.json, report)
    print(canonical_json(report['summary']), flush=True)
    return int(report['summary']['errors'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
