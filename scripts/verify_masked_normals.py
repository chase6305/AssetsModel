"""Independently inspect a source-SHA-locked corner-normal mask and its trial.

Only the stdlib surface audit's independent struct decoder is used. No
production smoothing, packing, preview, or comparison functions are imported.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct

from verify_surface_preservation import (read_container, read_accessor,
                                         verify_surfaces, matrix, IDENTITY)

ROOT = Path(__file__).resolve().parents[1]
ANGLE_EPSILON = 1e-4


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def unit(vector):
    length = math.sqrt(sum(value*value for value in vector))
    if not math.isfinite(length) or length <= 1e-15:
        raise AssertionError('Non-finite or zero normal')
    return tuple(value/length for value in vector), abs(length-1)


def dot(left, right):
    return sum(a*b for a, b in zip(left, right))


def angle(left, right):
    return math.degrees(math.acos(max(-1., min(1., dot(left, right)))))


def corner_rows(tree, binary, primitive, semantic):
    rows, schema, _code, _width = read_accessor(tree, binary, primitive['attributes'][semantic])
    if 'indices' in primitive:
        indices, index_schema, code, width = read_accessor(tree, binary, primitive['indices'])
        if index_schema[0] not in (5121, 5123, 5125) or width != 1:
            raise ValueError('Expected unsigned scalar indices')
        order = [struct.unpack('<'+code, row)[0] for row in indices]
    else:
        order = list(range(len(rows)))
    return [rows[index] for index in order], schema


def without_layout(tree):
    result = copy.deepcopy(tree)
    for key in ('buffers', 'bufferViews', 'accessors'):
        result.pop(key, None)
    for mesh in result['meshes']:
        for primitive in mesh['primitives']:
            primitive['attributes'] = sorted(primitive['attributes'])
            if 'indices' in primitive:
                primitive['indices'] = 'INDEX_LAYOUT'
    return result


def inspect_edges(edges):
    result = {'shared_edges': 0, 'open_edges': 0, 'nonmanifold_edges': 0,
              'same_direction_paired_edges': 0, 'cross_primitive_shared_edges': 0,
              'near_coplanar_endpoint_pairs': 0, 'near_coplanar_10_to_15_seams_added': 0,
              'near_coplanar_gap_growth_gt_1deg': 0, 'near_coplanar_new_gap_above_1deg': 0,
              'near_coplanar_gap_growth_gt_0_001deg': 0, 'near_coplanar_max_gap_growth_deg': 0.,
              'selected_boundary_endpoint_pairs': 0, 'selected_boundary_max_output_gap_deg': 0.,
              'selected_boundary_near_coplanar_max_output_gap_deg': 0.,
              'selected_boundary_smooth_geometry_max_output_gap_deg': 0.,
              'geometric_and_authored_30deg_hard_endpoint_pairs': 0, 'hard_endpoint_pairs_below_30deg_after': 0,
              'source_large_normal_gaps_reduced_on_geometrically_smooth_pairs': 0,
              'positive_gap_growth_examples': []}
    positive = []
    for key, entries in edges.items():
        if len(entries) == 1:
            result['open_edges'] += 1; continue
        if len(entries) != 2:
            result['nonmanifold_edges'] += 1; continue
        a, b = entries
        if a['start_position'] != b['end_position'] or a['end_position'] != b['start_position']:
            result['same_direction_paired_edges'] += 1; continue
        result['shared_edges'] += 1
        result['cross_primitive_shared_edges'] += a['primitive'] != b['primitive']
        if a['face_normal'] is None or b['face_normal'] is None:
            continue
        geometric_gap = angle(a['face_normal'], b['face_normal'])
        for ia, ib in ((0, 1), (1, 0)):
            old_gap = angle(a['old_normals'][ia], b['old_normals'][ib])
            new_gap = angle(a['new_normals'][ia], b['new_normals'][ib]); growth = new_gap-old_gap
            if a['selected'][ia] != b['selected'][ib]:
                result['selected_boundary_endpoint_pairs'] += 1
                result['selected_boundary_max_output_gap_deg'] = max(result['selected_boundary_max_output_gap_deg'], new_gap)
                if geometric_gap < 10:
                    result['selected_boundary_near_coplanar_max_output_gap_deg'] = max(result['selected_boundary_near_coplanar_max_output_gap_deg'], new_gap)
                if geometric_gap < 30-ANGLE_EPSILON:
                    result['selected_boundary_smooth_geometry_max_output_gap_deg'] = max(result['selected_boundary_smooth_geometry_max_output_gap_deg'], new_gap)
            if geometric_gap < 10:
                result['near_coplanar_endpoint_pairs'] += 1
                result['near_coplanar_max_gap_growth_deg'] = max(result['near_coplanar_max_gap_growth_deg'], growth)
                result['near_coplanar_10_to_15_seams_added'] += old_gap < 10 and new_gap > 15
                result['near_coplanar_gap_growth_gt_1deg'] += growth > 1+ANGLE_EPSILON
                result['near_coplanar_new_gap_above_1deg'] += old_gap <= 1+ANGLE_EPSILON and new_gap > 1+ANGLE_EPSILON
                result['near_coplanar_gap_growth_gt_0_001deg'] += growth > .001
                if growth > ANGLE_EPSILON:
                    positive.append({'geometric_gap_deg': geometric_gap, 'source_normal_gap_deg': old_gap,
                                     'output_normal_gap_deg': new_gap, 'gap_growth_deg': growth,
                                     'primitives': [a['primitive'], b['primitive']],
                                     'corners': [a['corners'][ia], b['corners'][ib]],
                                     'position': list(a['start_position'] if ia == 0 else a['end_position']),
                                     'selected': [a['selected'][ia], b['selected'][ib]]})
            if geometric_gap >= 30-ANGLE_EPSILON and old_gap >= 30-ANGLE_EPSILON:
                result['geometric_and_authored_30deg_hard_endpoint_pairs'] += 1
                result['hard_endpoint_pairs_below_30deg_after'] += new_gap < 30-ANGLE_EPSILON
            if geometric_gap < 30-ANGLE_EPSILON and old_gap >= 30 and new_gap < 30:
                result['source_large_normal_gaps_reduced_on_geometrically_smooth_pairs'] += 1
    result['positive_gap_growth_examples'] = sorted(positive, key=lambda row: row['gap_growth_deg'], reverse=True)[:12]
    return result


def verify_masked_update(source, output, mask):
    before, before_bin = read_container(source); after, after_bin = read_container(output)
    if without_layout(before) != without_layout(after):
        raise AssertionError('Material/node/scene/mesh/primitive metadata changed')
    geometric = verify_surfaces(source, output)
    chosen = {(row['mesh'], row['primitive']): row for row in mask['primitives']}
    if len(chosen) != len(mask['primitives']):
        raise AssertionError('Repeated primitive masks')
    available = {(mi, pi) for mi, mesh in enumerate(before['meshes']) for pi in range(len(mesh['primitives']))}
    if not chosen.keys() <= available:
        raise AssertionError('Mask references an absent primitive')
    counts = {'triangles': 0, 'position_corners_ordered_byte_exact': 0, 'selected_corners': 0,
              'changed_normal_corners': 0, 'unselected_normal_corners_byte_exact': 0,
              'source_opposed_normal_corners': 0, 'output_opposed_normal_corners': 0,
              'newly_opposed_normal_corners': 0, 'degenerate_triangles': 0,
              'max_source_normal_length_error': 0., 'max_output_normal_length_error': 0.}
    details = []; all_edges = defaultdict(list)
    # The reviewed files have one identity node per mesh. This also permits
    # checking contact edges across material primitives in a shared local frame.
    identity_nodes = all(matrix(node) == IDENTITY for node in before['nodes'])
    mesh_references = [node['mesh'] for node in before['nodes'] if 'mesh' in node]
    cross_primitive_supported = identity_nodes and len(set(mesh_references)) == len(mesh_references)
    for mi, mesh in enumerate(before['meshes']):
        for pi, primitive in enumerate(mesh['primitives']):
            target = after['meshes'][mi]['primitives'][pi]; key = (mi, pi)
            mask_row = chosen.get(key); selection = set(mask_row['selected_corner_indices']) if mask_row else set()
            if mask_row:
                if mask_row['selected_corner_indices'] != sorted(selection):
                    raise AssertionError('Mask corner list must be unique and ascending')
                actual_name = before['materials'][primitive['material']].get('name')
                if actual_name != mask_row['material_name']:
                    raise AssertionError('Mask material label differs from source primitive')
            source_normals = target_normals = None
            for semantic in primitive['attributes']:
                left, schema = corner_rows(before, before_bin, primitive, semantic)
                right, target_schema = corner_rows(after, after_bin, target, semantic)
                if schema != target_schema or len(left) != len(right):
                    raise AssertionError('Attribute representation/count changed: '+semantic)
                if semantic != 'NORMAL':
                    if left != right:
                        raise AssertionError('Ordered corner attribute changed: '+semantic)
                    if semantic == 'POSITION':
                        counts['position_corners_ordered_byte_exact'] += len(left)
                        position_rows = left
                else:
                    if schema != (5126, 'VEC3', False):
                        raise ValueError('Expected float32 XYZ normals')
                    if selection and (min(selection) < 0 or max(selection) >= len(left)):
                        raise AssertionError('Mask corner outside the original primitive')
                    if mask_row:
                        if len(selection) != mask_row['selected_corner_count'] or len(left) != mask_row['corner_count']:
                            raise AssertionError('Selected count differs from reviewed mask')
                        if sha(b''.join(left)) != mask_row['source_corner_normals_sha256']:
                            raise AssertionError('Source ordered NORMAL hash differs from reviewed mask')
                        if sha(b''.join(right)) != mask_row['expected_output_corner_normals_sha256']:
                            raise AssertionError('Output ordered NORMAL hash differs from reviewed mask')
                    for corner, (a, b) in enumerate(zip(left, right)):
                        if corner not in selection:
                            if a != b:
                                raise AssertionError('Unselected corner NORMAL byte changed')
                            counts['unselected_normal_corners_byte_exact'] += 1
                        counts['changed_normal_corners'] += a != b
                    source_normals = [unit(struct.unpack('<fff', raw)) for raw in left]
                    target_normals = [unit(struct.unpack('<fff', raw)) for raw in right]
            counts['selected_corners'] += len(selection)
            if primitive.get('mode', 4) != 4 or source_normals is None:
                continue
            positions = [struct.unpack('<fff', raw) for raw in position_rows]
            if len(positions) % 3:
                raise ValueError('Non-triangle corner count')
            edges = defaultdict(list); degenerate = 0
            for start in range(0, len(positions), 3):
                p = positions[start:start+3]
                u = tuple(p[1][i]-p[0][i] for i in range(3)); v = tuple(p[2][i]-p[0][i] for i in range(3))
                cross = (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
                length = math.sqrt(dot(cross, cross))
                gn = tuple(value/length for value in cross) if length > 1e-20 else None
                degenerate += gn is None
                for corner in range(start, start+3):
                    old, old_error = source_normals[corner]; new, new_error = target_normals[corner]
                    counts['max_source_normal_length_error'] = max(counts['max_source_normal_length_error'], old_error)
                    counts['max_output_normal_length_error'] = max(counts['max_output_normal_length_error'], new_error)
                    if new_error > 1e-5:
                        raise AssertionError('Non-unit output normal')
                    if gn is not None:
                        was_opposed = dot(gn, old) < -1e-4; now_opposed = dot(gn, new) < -1e-4
                        counts['source_opposed_normal_corners'] += was_opposed
                        counts['output_opposed_normal_corners'] += now_opposed
                        counts['newly_opposed_normal_corners'] += now_opposed and not was_opposed
                for local in range(3):
                    a = start+local; b = start+(local+1)%3
                    entry = {'primitive': [mi, pi], 'corners': [a, b], 'start_position': positions[a],
                             'end_position': positions[b], 'face_normal': gn,
                             'old_normals': [source_normals[a][0], source_normals[b][0]],
                             'new_normals': [target_normals[a][0], target_normals[b][0]],
                             'selected': [a in selection, b in selection]}
                    edge_key = tuple(sorted((positions[a], positions[b])))
                    edges[edge_key].append(entry)
                    if cross_primitive_supported:
                        all_edges[edge_key].append(entry)
            counts['triangles'] += len(positions)//3; counts['degenerate_triangles'] += degenerate
            details.append({'mesh': mi, 'primitive': pi, 'selected_corners': len(selection), 'edge_analysis': inspect_edges(edges)})
    if counts['newly_opposed_normal_corners']:
        raise AssertionError('New opposed normal directions introduced')
    return {'geometry': geometric, **counts, 'primitives': details,
            'cross_primitive_edge_analysis_supported': cross_primitive_supported,
            'whole_file_contact_edge_analysis': inspect_edges(all_edges) if cross_primitive_supported else None,
            'material_node_scene_metadata_exact': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-model', type=Path, required=True)
    parser.add_argument('--output-model', type=Path, required=True)
    parser.add_argument('--reference-model', type=Path, help='Optional immutable diagnosis snapshot, required byte-equal to actual source')
    parser.add_argument('--mask', type=Path, default=ROOT/'docs/quality/ur5-local-normal-mask.json')
    parser.add_argument('--json', type=Path, required=True)
    args = parser.parse_args(); mask_bytes = args.mask.read_bytes(); mask = json.loads(mask_bytes)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'checker_sha256': sha(Path(__file__).read_bytes()),
              'decoder_sha256': sha((ROOT/'scripts/verify_surface_preservation.py').read_bytes()),
              'mask_sha256': sha(mask_bytes), 'source_model': str(args.source_model.resolve()),
              'output_model': str(args.output_model.resolve()),
              'method': 'Independent struct accessor/ordered-corner comparison; exact metadata/materials and unselected N; every paired opposite-directed exact-coordinate shared edge inspected at both endpoints, within primitives and across primitives where identity node frames permit.',
              'near_coplanar_definition': 'Geometric face-normal angle <10 degrees; all normal-gap growth is reported, including growth >1 degree and source<=1/output>1 crossings. Positive examples use >0.0001 degree; quantization-sized growth separately visible in the maximum.',
              'hard_edge_definition': 'Geometric dihedral >=30 degrees and authored source endpoint-N gap >=30 degrees; output gap must retain >=30 degrees (angular epsilon 0.0001 degree).',
              'files': []}
    input_sha = {str(args.mask.resolve()): sha(mask_bytes)}
    for entry in mask['files']:
        source_path = args.source_model/entry['path']; output_path = args.output_model/entry['path']
        source = source_path.read_bytes(); output = output_path.read_bytes()
        if sha(source) != entry['source_sha256']:
            raise AssertionError('Actual source SHA differs from reviewed mask')
        input_sha[str(source_path.resolve())] = sha(source); input_sha[str(output_path.resolve())] = sha(output)
        reference_sha = None
        if args.reference_model:
            reference_path = args.reference_model/entry['path']; reference_bytes = reference_path.read_bytes()
            if reference_bytes != source:
                raise AssertionError('Repository source differs from immutable diagnosis snapshot')
            reference_sha = sha(reference_bytes); input_sha[str(reference_path.resolve())] = reference_sha
        row = {'path': entry['path'], 'source_sha256': sha(source), 'output_sha256': sha(output),
               'diagnosis_reference_sha256': reference_sha,
               **verify_masked_update(source, output, entry)}
        report['files'].append(row)
    report['input_sha256'] = input_sha
    report['inputs_changed_during_audit'] = [p for p, value in input_sha.items() if sha(Path(p).read_bytes()) != value]
    analyses = [primitive['edge_analysis'] for row in report['files'] for primitive in row['primitives']]
    whole = [row['whole_file_contact_edge_analysis'] for row in report['files'] if row['whole_file_contact_edge_analysis'] is not None]
    report['summary'] = {'files': len(report['files']),
                         'selected_corners': sum(row['selected_corners'] for row in report['files']),
                         'changed_normal_corners': sum(row['changed_normal_corners'] for row in report['files']),
                         'newly_opposed_normal_corners': sum(row['newly_opposed_normal_corners'] for row in report['files']),
                         'hard_endpoint_pairs_merged_below_30deg': sum(a['hard_endpoint_pairs_below_30deg_after'] for a in analyses),
                         'near_coplanar_gap_growth_gt_1deg': sum(a['near_coplanar_gap_growth_gt_1deg'] for a in analyses),
                         'near_coplanar_new_gap_above_1deg': sum(a['near_coplanar_new_gap_above_1deg'] for a in analyses),
                         'near_coplanar_max_gap_growth_deg': max((a['near_coplanar_max_gap_growth_deg'] for a in analyses), default=0),
                         'near_coplanar_gap_growth_gt_0_001deg': sum(a['near_coplanar_gap_growth_gt_0_001deg'] for a in analyses)}
    report['summary']['whole_file_contact_edges'] = {
        'files_supported': len(whole),
        'cross_primitive_shared_edges': sum(a['cross_primitive_shared_edges'] for a in whole),
        'geometric_and_authored_30deg_hard_endpoint_pairs': sum(a['geometric_and_authored_30deg_hard_endpoint_pairs'] for a in whole),
        'hard_endpoint_pairs_merged_below_30deg': sum(a['hard_endpoint_pairs_below_30deg_after'] for a in whole),
        'near_coplanar_max_gap_growth_deg': max((a['near_coplanar_max_gap_growth_deg'] for a in whole), default=0),
        'near_coplanar_gap_growth_gt_0_001deg': sum(a['near_coplanar_gap_growth_gt_0_001deg'] for a in whole),
        'near_coplanar_gap_growth_gt_1deg': sum(a['near_coplanar_gap_growth_gt_1deg'] for a in whole),
        'near_coplanar_new_gap_above_1deg': sum(a['near_coplanar_new_gap_above_1deg'] for a in whole),
        'selected_boundary_near_coplanar_max_output_gap_deg': max((a['selected_boundary_near_coplanar_max_output_gap_deg'] for a in whole), default=0),
        'selected_boundary_smooth_geometry_max_output_gap_deg': max((a['selected_boundary_smooth_geometry_max_output_gap_deg'] for a in whole), default=0)}
    issues = any(report['summary'][key] for key in ('newly_opposed_normal_corners','hard_endpoint_pairs_merged_below_30deg','near_coplanar_gap_growth_gt_0_001deg'))
    issues |= any(report['summary']['whole_file_contact_edges'][key] for key in ('hard_endpoint_pairs_merged_below_30deg','near_coplanar_gap_growth_gt_0_001deg'))
    report['status'] = 'review_required' if report['inputs_changed_during_audit'] or issues else 'passed'
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    args.json.parent.mkdir(parents=True, exist_ok=True); args.json.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'status': report['status'], **report['summary']}))


if __name__ == '__main__':
    main()
