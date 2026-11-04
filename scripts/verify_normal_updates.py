"""Independently verify normal-only GLB edits using only the Python standard library.

Geometry and retained attribute checks decode oriented accessor corners with
struct, independently of the conversion, smoothing and production GLB readers.
The comparison requires mesh/primitive layouts to remain stable; it does not
replace the separate primitive-batching multiset validation.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct

_COMPONENTS = {5120: ('b', 1), 5121: ('B', 1), 5122: ('h', 2), 5123: ('H', 2), 5125: ('I', 4), 5126: ('f', 4)}
_WIDTHS = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT2': 4, 'MAT3': 9, 'MAT4': 16}


def independent_read_glb(payload):
    """Use only stdlib struct, independent of the production GLB/accessor readers."""
    magic, version, length = struct.unpack_from('<III', payload)
    if (magic, version, length) != (0x46546C67, 2, len(payload)):
        raise ValueError('Invalid GLB container')
    chunks = {}
    offset = 12
    while offset < length:
        size, kind = struct.unpack_from('<II', payload, offset)
        offset += 8
        if size % 4 or offset + size > length:
            raise ValueError('Invalid GLB chunk')
        chunks[kind] = payload[offset:offset + size]
        offset += size
    return json.loads(chunks[0x4E4F534A]), chunks[0x004E4942]


def independent_accessor(tree, binary, index):
    accessor = tree['accessors'][index]
    if 'sparse' in accessor:
        raise ValueError('Sparse accessor requires independent sparse decoding')
    view = tree['bufferViews'][accessor['bufferView']]
    code, size = _COMPONENTS[accessor['componentType']]
    width = _WIDTHS[accessor['type']]
    packed = size * width
    stride = view.get('byteStride', packed)
    start = view.get('byteOffset', 0) + accessor.get('byteOffset', 0)
    rows = [binary[start + i * stride:start + i * stride + packed] for i in range(accessor['count'])]
    if any(len(row) != packed for row in rows):
        raise ValueError('Truncated accessor')
    return rows, code, width


def independent_corner_rows(tree, binary, primitive, semantic):
    rows, code, width = independent_accessor(tree, binary, primitive['attributes'][semantic])
    if 'indices' in primitive:
        indices, index_code, index_width = independent_accessor(tree, binary, primitive['indices'])
        if index_width != 1:
            raise ValueError('Non-scalar index accessor')
        order = [struct.unpack('<' + index_code, row)[0] for row in indices]
    else:
        order = list(range(len(rows)))
    return [rows[index] for index in order], code, width


def verify_normal_update(before, after, eligible_primitives=None):
    """Check oriented corner bytes and authored-normal protections, not just bounds."""
    old, old_bin = independent_read_glb(before)
    new, new_bin = independent_read_glb(after)
    selected = None if eligible_primitives is None else frozenset(tuple(pair) for pair in eligible_primitives)
    available = {(mi, pi) for mi, mesh in enumerate(old["meshes"]) for pi, _ in enumerate(mesh["primitives"])}
    if selected is not None and not selected <= available:
        raise ValueError("Selected primitive does not exist in source GLB")
    stripped = []
    for tree in (old, new):
        copy_tree = copy.deepcopy(tree)
        for key in ('buffers', 'bufferViews', 'accessors'):
            copy_tree.pop(key, None)
        for mesh in copy_tree['meshes']:
            if mesh.get('weights'):
                raise ValueError('Static morph weights need independent morph decoding')
            for primitive in mesh['primitives']:
                if primitive.get('targets'):
                    raise ValueError('Morph target accessors need independent morph decoding')
                primitive['attributes'] = sorted(primitive['attributes'])
                primitive['indices'] = 'INDEX_LAYOUT'
        stripped.append(copy_tree)
    if stripped[0] != stripped[1]:
        raise AssertionError('Non-layout JSON metadata changed')
    counts = {'primitives': 0, 'triangles': 0, 'line_segments': 0, 'position_corners_byte_exact': 0,
              'normal_corners_changed_bytes': 0, 'protected_faces': 0, 'authored_smooth_primitives_retained': 0,
              'protected_faces_in_flat_primitives': 0, 'protected_faces_in_authored_smooth_primitives': 0,
              'source_opposed_normal_corners': 0, 'output_opposed_normal_corners': 0,
              'newly_opposed_normal_corners': 0, 'max_normal_length_error': 0.0,
              'unselected_primitives_checked': 0, 'unselected_normal_corners_byte_exact': 0}
    for mi, (old_mesh, new_mesh) in enumerate(zip(old['meshes'], new['meshes'])):
        for pi, (op, np_) in enumerate(zip(old_mesh['primitives'], new_mesh['primitives'])):
            counts['primitives'] += 1
            is_selected = selected is None or (mi, pi) in selected
            if not is_selected:
                counts['unselected_primitives_checked'] += 1
            for semantic in op['attributes']:
                old_accessor = old['accessors'][op['attributes'][semantic]]
                new_accessor = new['accessors'][np_['attributes'][semantic]]
                if bool(old_accessor.get('normalized', False)) != bool(new_accessor.get('normalized', False)):
                    raise AssertionError(f'Attribute normalization semantics changed: {semantic}')
                old_rows, old_code, old_width = independent_corner_rows(old, old_bin, op, semantic)
                new_rows, new_code, new_width = independent_corner_rows(new, new_bin, np_, semantic)
                if (old_code, old_width, len(old_rows)) != (new_code, new_width, len(new_rows)):
                    raise AssertionError(f'Attribute representation changed: {semantic}')
                if semantic != 'NORMAL' and old_rows != new_rows:
                    raise AssertionError(f'Oriented attribute corner bytes changed: {semantic}')
                if semantic == 'NORMAL' and not is_selected:
                    if old_rows != new_rows:
                        raise AssertionError(f'Unselected authored normal bytes changed at mesh {mi}, primitive {pi}')
                    counts['unselected_normal_corners_byte_exact'] += len(old_rows)
                if semantic == 'POSITION':
                    counts['position_corners_byte_exact'] += len(old_rows)
                else:
                    continue
                mode = op.get('mode', 4)
                if mode == 1:
                    counts['line_segments'] += len(old_rows) // 2
                if mode != 4:
                    continue
                counts['triangles'] += len(old_rows) // 3
                if 'NORMAL' not in op['attributes']:
                    continue
                if (old_code, old_width) != ('f', 3):
                    raise ValueError('Geometry audit expects float32 XYZ positions')
                positions = [struct.unpack('<fff', row) for row in old_rows]
                old_n, nc, nw = independent_corner_rows(old, old_bin, op, 'NORMAL')
                new_n, new_nc, new_nw = independent_corner_rows(new, new_bin, np_, 'NORMAL')
                if (nc, nw, new_nc, new_nw) != ('f', 3, 'f', 3):
                    raise ValueError('Normal audit expects float32 XYZ normals')
                old_normals = [struct.unpack('<fff', row) for row in old_n]
                new_normals = [struct.unpack('<fff', row) for row in new_n]
                counts['normal_corners_changed_bytes'] += sum(a != b for a, b in zip(old_n, new_n))
                smooth = any(math.dist(old_normals[i], old_normals[j]) > 1e-6
                             for i in range(0, len(old_normals), 3) for j in (i + 1, i + 2))
                if smooth:
                    if old_n != new_n:
                        raise AssertionError('Authored smooth normal group changed')
                    counts['authored_smooth_primitives_retained'] += 1
                for offset in range(0, len(positions), 3):
                    a, b, c = positions[offset:offset + 3]
                    u, v = [b[i]-a[i] for i in range(3)], [c[i]-a[i] for i in range(3)]
                    cross = (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
                    length = math.sqrt(sum(x*x for x in cross))
                    if length <= 1e-20:
                        raise ValueError('Degenerate source triangle')
                    face = [x/length for x in cross]
                    old_unit = []
                    for normal in old_normals[offset:offset + 3]:
                        normal_length = math.sqrt(sum(x*x for x in normal))
                        old_unit.append([x/normal_length for x in normal])
                    protected = max(math.dist(normal, face) for normal in old_unit) > 2e-3
                    if protected:
                        counts['protected_faces'] += 1
                        counts['protected_faces_in_authored_smooth_primitives' if smooth else 'protected_faces_in_flat_primitives'] += 1
                        if old_n[offset:offset+3] != new_n[offset:offset+3]:
                            raise AssertionError('Direction-outlier normal bytes changed')
                    for corner, (prior, current) in enumerate(zip(old_unit, new_normals[offset:offset + 3])):
                        normal_length = math.sqrt(sum(x*x for x in current))
                        if not math.isfinite(normal_length) or normal_length <= 0:
                            raise AssertionError('Non-finite/zero output normal')
                        if abs(normal_length-1) > 1e-3 and old_n[offset+corner] != new_n[offset+corner]:
                            raise AssertionError('Refinement introduced a non-unit normal')
                        counts['max_normal_length_error'] = max(counts['max_normal_length_error'], abs(normal_length-1))
                        old_dot = sum(x*y for x, y in zip(prior, face))
                        new_dot = sum(x*y/normal_length for x, y in zip(current, face))
                        counts['source_opposed_normal_corners'] += int(old_dot < -1e-4)
                        counts['output_opposed_normal_corners'] += int(new_dot < -1e-4)
                        if old_dot >= -1e-4 and new_dot < -1e-4:
                            counts['newly_opposed_normal_corners'] += 1
    if counts['newly_opposed_normal_corners']:
        raise AssertionError('Refinement introduced shading normals opposed to source winding')
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--target-root', type=Path, required=True)
    parser.add_argument('--files', type=Path, nargs='+', required=True)
    parser.add_argument('--selection', type=Path, help='JSON mapping relative file paths to [mesh, primitive] pairs')
    parser.add_argument('--json', type=Path, required=True)
    args = parser.parse_args()
    source, target = args.source_root.resolve(), args.target_root.resolve()
    selections = json.loads(args.selection.read_text()) if args.selection else None
    rows = []
    for relative in args.files:
        old_path, new_path = (source/relative).resolve(), (target/relative).resolve()
        if not old_path.is_relative_to(source) or not new_path.is_relative_to(target):
            parser.error('Files must remain inside the corresponding resource roots')
        row = {'path': relative.as_posix()}
        try:
            old, new = old_path.read_bytes(), new_path.read_bytes()
            selected = None if selections is None else selections[relative.as_posix()]
            row.update(status='passed', source_sha256=hashlib.sha256(old).hexdigest(),
                       target_sha256=hashlib.sha256(new).hexdigest(),
                       checks=verify_normal_update(old, new, eligible_primitives=selected))
        except (OSError, ValueError, KeyError, IndexError, AssertionError, struct.error) as exc:
            row.update(status='failed', error=str(exc))
        rows.append(row)
        print(row['status'], row['path'], flush=True)
    summary = {'files': len(rows), 'errors': sum(r['status'] != 'passed' for r in rows)}
    report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'summary': summary, 'files': rows}
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2)+'\n')
    return int(summary['errors'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
