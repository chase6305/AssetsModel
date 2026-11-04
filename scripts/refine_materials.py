"""Refine the existing robot visual GLB materials without changing geometry.

Dry run by default. --apply requires a new backup directory. Only the material
array is edited; the embedded BIN and every other glTF field stay identical.
The palette approximates product photography, not certified manufacturer paint.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import struct
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from glb_format import read_glb, validate_glb

ROOT = Path(__file__).resolve().parents[1]
VENDORS = ('ABB', 'AE', 'Agile', 'Fanuc', 'Franka', 'Hans', 'KUKA', 'Motoman', 'Rokae', 'UniversalRobots')
PROFILE = 'assetsmodel-brand-materials-v1'


def srgb(hex_rgb):
    values = [int(hex_rgb[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return [round(v / 12.92 if v <= 0.04045 else ((v + .055) / 1.055) ** 2.4, 8) for v in values] + [1.0]


PALETTE = {
    'peitian_yellow': srgb('EFB719'),
    'kuka_orange': srgb('EF7421'),
    'kuka_agilus_white': srgb('E8EAE7'),
    'kuka_dark_interface': srgb('303437'),
    'fanuc_yellow': srgb('F4CB16'),
    'motoman_blue': srgb('006EB8'),
    'ur_blue': srgb('79AAC7'),
    'rokae_soft_cyan': srgb('B9DDEB'),
    'white_paint': srgb('E4E6E2'),
    'franka_shell': srgb('ECEEEF'),
    'graphite': srgb('343B40'),
    'black_polymer': srgb('20262C'),
    'aluminium': srgb('BFC5CA'),
    'fanuc_m20_casting': srgb('424444'),
    'fanuc_m710_casting': srgb('303437'),
    'fanuc_r2000_casting': srgb('363A3D'),
    'fanuc_dark_interface': srgb('303437'),
}
# Same-model manufacturer photographs support these component-specific colors.
# Match both the model path and the original named material. Brand-wide rules
# would incorrectly affect other generations, black rings and painted shells.
MODEL_FINISHES = {
    ('Fanuc', 'M_20iA', 'BaseLink.glb', 'RAL9005_008-material'): ('paint_dark', 'fanuc_m20_casting'),
    ('Fanuc', 'M_710iC_70', 'BaseLink.glb', 'link_01_part_000_mtl_001_001-material'): ('paint_dark', 'fanuc_m710_casting'),
    ('Fanuc', 'R_2000iC_165F', 'BaseLink.glb', 'link_01_part_000_mtl_001_003-material'): ('paint_dark', 'fanuc_r2000_casting'),
    ('Fanuc', 'M_20iA', 'Link6.glb', 'Material_003-material'): ('metal', 'fanuc_dark_interface'),
    ('Fanuc', 'M_710iC_70', 'Link6.glb', 'link_01_part_000_mtl_001_001-material'): ('metal', 'fanuc_dark_interface'),
    ('Fanuc', 'R_2000iC_165F', 'Link6.glb', 'link_01_part_000_mtl_001_008-material'): ('metal', 'fanuc_dark_interface'),
    ('Hans', 'E05', 'Link6.glb', 'Material-material'): ('brushed_metal', 'aluminium'),
    ('Hans', 'E10', 'Link6.glb', 'Material_033-material'): ('brushed_metal', 'aluminium'),
    ('Rokae', 'SR3', 'Link3.glb', 'Material_004-material'): ('plastic', 'rokae_soft_cyan'),
    ('KUKA', 'KR10_R900_2', 'Link1.glb', 'link_01_part_000_mtl_001_001-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR10_R900_2', 'Link2.glb', 'link_01_part_000_mtl_001-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR10_R900_2', 'Link3.glb', 'link_01_part_000_mtl_001_001-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR10_R900_2', 'Link4.glb', 'link_01_part_000_mtl_001-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR10_R900_2', 'Link5.glb', 'link_01_part_000_mtl_001-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR10_R1100_2', 'Link1.glb', 'link_01_part_000_mtl_001_003-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR10_R1100_2', 'Link2.glb', 'link_01_part_000_mtl_001_005-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR10_R1100_2', 'Link3.glb', 'link_01_part_000_mtl_001_006-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR10_R1100_2', 'Link4.glb', 'link_01_part_000_mtl_001_010-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR10_R1100_2', 'Link5.glb', 'link_01_part_000_mtl_001_011-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR3_R540', 'Link1.glb', 'link_01_part_000_mtl_001-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR3_R540', 'Link2.glb', 'link_01_part_000_mtl_001-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR3_R540', 'Link3.glb', 'link_01_part_000_mtl_001-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR3_R540', 'Link4.glb', 'link_01_part_000_mtl_001-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR3_R540', 'Link5.glb', 'link_01_part_000_mtl_001-material'): ('paint', 'kuka_agilus_white'),
    ('KUKA', 'KR10_R900_2', 'Link6.glb', 'link_06_part_000_mtl_001-material'): ('metal', 'kuka_dark_interface'),
    ('KUKA', 'KR10_R1100_2', 'Link6.glb', 'link_06_part_000_mtl_003-material'): ('metal', 'kuka_dark_interface'),
    ('KUKA', 'KR3_R540', 'Link6.glb', 'link_06_part_000_mtl-material'): ('metal', 'kuka_dark_interface'),
}
# Each entry is (metallic, perceptual roughness), matching glTF 2.0 semantics.
FINISH = {
    'paint': (0.0, .36), 'paint_dark': (0.0, .46), 'plastic': (0.0, .46),
    'rubber': (0.0, .68), 'metal': (.82, .27), 'brushed_metal': (.72, .34),
    'label': (0.0, .48), 'control': (0.0, .30),
}


def decide(vendor, path, material):
    """Classify source CAD groups; retain existing face-to-material assignment."""
    pbr = material.get('pbrMetallicRoughness', {})
    color = pbr.get('baseColorFactor', [1, 1, 1, 1])
    r, g, b = color[:3]
    name = material.get('name', '').lower()
    if 'baseColorTexture' in pbr:
        return None  # Do not recolor a baked label or texture.
    if not all(math.isfinite(float(v)) and 0 <= v <= 1 for v in color):
        raise ValueError(f'Invalid material color in {path}')
    if len(path.parts) >= 5 and path.parts[-3:-1] == ('Visual', 'glb') and path.parts[-5] == vendor:
        specific = MODEL_FINISHES.get((vendor, path.parts[-4], path.name, material.get('name')))
        if specific is not None:
            return specific
    saved = material.get('extras', {}).get('assetsmodel_material')
    if saved and saved.get('profile') == PROFILE:
        if vendor == 'AE' and saved['role'] == 'paint' and saved.get('palette') is None:
            return 'paint', 'peitian_yellow'
        return saved['role'], saved.get('palette')
    if vendor == 'KUKA':
        if r > .4 and r > g * 1.6 and g > b * 1.5 and g > .08:
            return 'paint', 'kuka_orange'
        if r > .4 and g < .05 and b < .05:
            return 'label', None
        if max(r, g, b) < .04:
            return 'rubber', None
        if max(r, g, b) >= .5 or ('link6' in path.stem.lower() and max(r, g, b) > .2):
            return 'metal', None
        return 'paint_dark', None
    if vendor == 'UniversalRobots':
        if 'urblue' in name or 'blau' in name or (b > r * 1.2 and b > g * 1.1):
            return 'plastic', 'ur_blue'
        if 'black' in name or 'scheibe' in name or max(r, g, b) < .09:
            return 'rubber', None
        if 'linkgrey' in name or 'rohr' in name or max(r, g, b) > .32:
            return 'brushed_metal', 'aluminium'
        return 'paint_dark', None
    if vendor == 'Motoman':
        if b > .3 and b > r * 3 and b > g * 3:
            return 'paint', 'motoman_blue'
        return 'paint_dark', None
    if vendor == 'Fanuc':
        if r > .7 and g > .4 and b < .25:
            return 'paint', 'fanuc_yellow'
        if max(r, g, b) < .03:
            return 'paint_dark', None
        if 'link6' in path.stem.lower():
            return 'metal', None
        return 'paint_dark', None
    if vendor == 'ABB':
        if 'white' in name:
            return 'paint', 'white_paint'
        if r > .4 and g < .1 and b < .1:
            return 'label', None
        return 'paint_dark', None
    if vendor == 'AE':
        return ('paint', 'peitian_yellow') if r > .5 and g > .5 else ('paint_dark', None)
    if vendor == 'Agile':
        if 'link7' in path.stem.lower() and max(r, g, b) > .4:
            return 'metal', None
        return ('paint', None) if max(r, g, b) > .1 else ('plastic', None)
    if vendor == 'Hans':
        return ('plastic', 'franka_shell') if min(r, g, b) > .5 else ('rubber', None)
    if vendor == 'Rokae':
        if min(r, g, b) > .8:
            return 'plastic', 'franka_shell'
        if b > r * 1.15:
            return 'plastic', None
        if 'metal' in name or 'rohr' in name or ('link6' in path.stem.lower() and r > .4):
            return 'metal', None
        return 'rubber', None
    if vendor == 'Franka':
        if 'color_192_192_192' in name or (path.stem in ('finger', 'hand') and min(r, g, b) > .8 and max(r, g, b) - min(r, g, b) > .01):
            return 'metal', 'aluminium'
        if 'color_0_174_255' in name:
            # White in this source export; leave its status light neutral.
            return 'control', None
        if (r - min(g, b) > .15 or g - min(r, b) > .15 or b - min(r, g) > .15):
            return 'control', None
        if min(r, g, b) > .8:
            return 'plastic', 'franka_shell'
        if max(r, g, b) < .3:
            return 'plastic', 'graphite'
        return 'plastic', None
    return None


def refine(tree, path, vendor):
    result, changes = copy.deepcopy(tree), []
    for index, material in enumerate(result.get('materials', [])):
        choice = decide(vendor, path, material)
        if choice is None:
            continue
        role, palette = choice
        before = copy.deepcopy(material)
        pbr = material.setdefault('pbrMetallicRoughness', {})
        pbr['metallicFactor'], pbr['roughnessFactor'] = FINISH[role]
        if palette is not None:
            alpha = pbr.get('baseColorFactor', [1, 1, 1, 1])[3]
            pbr['baseColorFactor'] = PALETTE[palette][:3] + [alpha]
        material.setdefault('extras', {})['assetsmodel_material'] = {'profile': PROFILE, 'role': role, 'palette': palette}
        if material != before:
            changes.append({'index': index, 'name': material.get('name'), 'role': role, 'palette': palette,
                            'before': before, 'after': copy.deepcopy(material)})
    return result, changes


def encode_glb(tree, binary):
    body = json.dumps(tree, separators=(',', ':'), ensure_ascii=True).encode()
    body += b' ' * (-len(body) % 4)
    return (struct.pack('<III', 0x46546C67, 2, 28 + len(body) + len(binary))
            + struct.pack('<II', len(body), 0x4E4F534A) + body
            + struct.pack('<II', len(binary), 0x004E4942) + binary)


def assert_material_only(before, after, original_bin, new_bin):
    if original_bin != new_bin:
        raise ValueError('Embedded BIN changed')
    old, new = copy.deepcopy(before), copy.deepcopy(after)
    old.pop('materials', None)
    new.pop('materials', None)
    if old != new:
        raise ValueError('Non-material glTF fields changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--vendors', nargs='+', choices=VENDORS, default=list(VENDORS))
    parser.add_argument('--include-imported', action='store_true',
                        help='Also retune models with import provenance; excluded by default to retain upstream finishes')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.apply and args.backup_dir is None:
        parser.error('--apply requires --backup-dir')
    if args.apply:
        backup = args.backup_dir.resolve()
        if backup.is_relative_to(root) or root.is_relative_to(backup):
            parser.error('Backup must be outside the asset root')
    files, roles, changed, skipped_imported = [], Counter(), 0, 0
    for vendor in args.vendors:
        for path in sorted((root / 'RobotModel' / vendor).glob('*/Visual/glb/*.glb')):
            if (path.parents[2] / 'provenance.json').is_file() and not args.include_imported:
                skipped_imported += 1
                continue
            data = path.read_bytes()
            original, binary = read_glb(data)
            target, edits = refine(original, path, vendor)
            record = {'path': path.relative_to(root).as_posix(), 'status': 'unchanged',
                      'before_sha256': hashlib.sha256(data).hexdigest(),
                      'bin_sha256': hashlib.sha256(binary).hexdigest(), 'materials': edits}
            if edits:
                encoded = encode_glb(target, binary)
                actual, actual_bin = read_glb(encoded)
                assert_material_only(original, actual, binary, actual_bin)
                info = validate_glb(encoded)
                record.update(status='applied' if args.apply else 'proposed',
                              after_sha256=hashlib.sha256(encoded).hexdigest(),
                              geometry_unchanged=True, non_material_json_unchanged=True, triangles=info['faces'])
                if args.apply:
                    copy_path = backup / path.relative_to(root)
                    if copy_path.exists() and copy_path.read_bytes() != data:
                        raise ValueError(f'Backup already has different content: {copy_path}')
                    copy_path.parent.mkdir(parents=True, exist_ok=True)
                    copy_path.write_bytes(data)
                    path.write_bytes(encoded)
                changed += 1
                roles.update(edit['role'] for edit in edits)
            files.append(record)
    summary = {'mode': 'apply' if args.apply else 'dry-run', 'visual_glbs': len(files), 'changed_glbs': changed,
               'imported_visual_glbs_excluded': skipped_imported,
               'changed_materials': sum(roles.values()), 'roles': dict(roles),
               'all_changed_geometry_unchanged': all(x.get('geometry_unchanged', True) for x in files),
               'profile': PROFILE, 'palette_linear_rgba': PALETTE}
    report = {'summary': summary, 'generated_at': datetime.now(timezone.utc).isoformat(), 'files': files}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
