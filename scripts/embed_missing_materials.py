"""Embed URDF visual colors only where a referenced GLB triangle lacks material.

All references are reconciled before planning writes. Conflicting colors for a
shared mesh fail; optional, unreferenced STL-derived GLBs are never considered.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from glb_format import read_glb, validate_glb
from refine_materials import ROOT, VENDORS, encode_glb, refine


def visual_rgba(robot, visual):
    material = visual.find('material')
    if material is None:
        raise ValueError('Referenced unmaterialed visual has no URDF material')
    color = material.find('color')
    if color is None:
        name = material.get('name')
        matches = [m for m in robot.findall('material') if m.get('name') == name]
        if len(matches) == 1:
            color = matches[0].find('color')
    if color is None:
        raise ValueError('Referenced unmaterialed visual has no URDF color')
    values = [float(x) for x in color.get('rgba', '').split()]
    if len(values) != 4 or not all(math.isfinite(x) and 0 <= x <= 1 for x in values):
        raise ValueError('Invalid URDF visual color')
    return material.get('name', 'URDFVisual'), values


def missing_primitives(tree):
    return [(mi, pi) for mi, mesh in enumerate(tree.get('meshes', []))
            for pi, primitive in enumerate(mesh['primitives'])
            if primitive.get('mode', 4) == 4 and 'material' not in primitive]


def collect_references(root, vendors=VENDORS):
    root = Path(root).resolve()
    refs = defaultdict(list)
    for vendor in vendors:
        for urdf in sorted((root / 'RobotModel' / vendor).rglob('*.urdf')):
            robot = ET.parse(urdf).getroot()
            for visual in robot.findall('./link/visual'):
                mesh = visual.find('geometry/mesh')
                if mesh is None:
                    continue
                path = (urdf.parent / mesh.get('filename', '')).resolve()
                if path.suffix != '.glb':
                    continue
                if not path.is_relative_to(urdf.parent.resolve()):
                    raise ValueError(f'Visual path escapes model: {urdf}')
                tree, _ = read_glb(path)
                if not missing_primitives(tree):
                    continue
                name, color = visual_rgba(robot, visual)
                refs[path].append({'urdf': urdf.relative_to(root).as_posix(), 'name': name, 'rgba': color})
    for path, uses in refs.items():
        color = uses[0]['rgba']
        if any(any(abs(a - b) > 1e-8 for a, b in zip(color, x['rgba'])) for x in uses[1:]):
            raise ValueError(f'Conflicting URDF colors for {path}: {uses}')
    return refs


def embed(tree, path, references):
    target = copy.deepcopy(tree)
    missing = missing_primitives(target)
    if not missing:
        return target, []
    source = references[0]
    material = {'name': source['name'], 'pbrMetallicRoughness': {
        'baseColorFactor': source['rgba'], 'metallicFactor': 0.0, 'roughnessFactor': .36},
        'extras': {'urdf_material_source': copy.deepcopy(references)}}
    if source['rgba'][3] < 1:
        material['alphaMode'] = 'BLEND'
    vendor = path.parts[path.parts.index('RobotModel') + 1]
    calibrated, _ = refine({'materials': [material]}, path, vendor)
    material = calibrated['materials'][0]
    index = len(target.get('materials', []))
    target.setdefault('materials', []).append(material)
    for mi, pi in missing:
        target['meshes'][mi]['primitives'][pi]['material'] = index
    return target, missing


def assert_only_added_material_bindings(before, after, missing, original_bin, new_bin):
    if original_bin != new_bin:
        raise ValueError('BIN changed')
    expected = copy.deepcopy(after)
    original_count = len(before.get('materials', []))
    if len(after.get('materials', [])) != original_count + 1:
        raise ValueError('Expected exactly one new material')
    if expected['materials'][:original_count] != before.get('materials', []):
        raise ValueError('Existing material changed')
    for mi, pi in missing:
        primitive = expected['meshes'][mi]['primitives'][pi]
        if primitive.pop('material', None) != original_count:
            raise ValueError('Incorrect new material binding')
    if 'materials' in before:
        expected['materials'] = copy.deepcopy(before['materials'])
    else:
        expected.pop('materials', None)
    if expected != before:
        raise ValueError('Non-material-binding JSON changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--vendors', nargs='+', choices=VENDORS, default=list(VENDORS))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.apply and args.backup_dir is None:
        parser.error('--apply requires --backup-dir')
    backup = args.backup_dir.resolve() if args.backup_dir else None
    if backup and (backup.is_relative_to(root) or root.is_relative_to(backup)):
        parser.error('Backup must be outside the asset root')
    references = collect_references(root, args.vendors)
    plans, records = [], []
    for path, refs in sorted(references.items()):
        old = path.read_bytes()
        original, binary = read_glb(old)
        target, missing = embed(original, path, refs)
        new = encode_glb(target, binary)
        parsed, parsed_bin = read_glb(new)
        assert_only_added_material_bindings(original, parsed, missing, binary, parsed_bin)
        validate_glb(new)
        copy_path = backup / path.relative_to(root) if backup else None
        if args.apply and copy_path.exists() and copy_path.read_bytes() != old:
            raise ValueError(f'Backup already has different content: {copy_path}')
        plans.append((path, copy_path, old, new))
        records.append({'path': path.relative_to(root).as_posix(), 'urdf_references': refs,
                        'added_material_bindings': [{'mesh': m, 'primitive': p} for m, p in missing],
                        'before_sha256': hashlib.sha256(old).hexdigest(),
                        'after_sha256': hashlib.sha256(new).hexdigest(),
                        'bin_sha256': hashlib.sha256(binary).hexdigest(), 'bin_unchanged': True,
                        'only_material_and_missing_bindings_changed': True,
                        'embedded_material': target['materials'][-1]})
    if args.apply:
        for path, copy_path, old, new in plans:
            copy_path.parent.mkdir(parents=True, exist_ok=True)
            copy_path.write_bytes(old)
            path.write_bytes(new)
    summary = {'mode': 'apply' if args.apply else 'dry-run', 'glbs': len(plans),
               'triangle_material_bindings': sum(len(x['added_material_bindings']) for x in records),
               'all_bin_unchanged': True, 'only_material_and_missing_bindings_changed': True}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({'summary': summary, 'generated_at': datetime.now(timezone.utc).isoformat(),
                                     'files': records}, indent=2) + '\n')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
