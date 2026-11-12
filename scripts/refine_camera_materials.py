"""Trial satin camera housings, with optional neutral polymer USB plugs.

Dry-run by default. Only ten explicitly listed visual resources are eligible;
collision resources, URDFs and optional STLs are outside this tool's scope.
The finishes are visualization approximations, not measured manufacturer PBR.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from glb_format import read_glb, validate_glb
from refine_materials import encode_glb, srgb

ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'assetsmodel-camera-materials-v1'
MARKER = 'assetsmodel_camera_material'
BODY_MODELS = ('405', '415', '435', '436', '455', '585')
PLUG_MODELS = ('405', '415', '435', '436')
BODY_FINISH = {'metallicFactor': .65, 'roughnessFactor': .38}
MODEL_FINISHES = {'D455': {'metallicFactor': .65, 'roughnessFactor': .54}}
SOURCE_COMMIT = '7a87097c499e3b0b69518d54a8e7a9ac1924e87c'
SOURCE_PREFIX = (f'https://github.com/realsenseai/realsense-ros/blob/{SOURCE_COMMIT}/'
                 'realsense2_description/urdf/')
SOURCE_URLS = [SOURCE_PREFIX + f'_d{model}.urdf.xacro' for model in BODY_MODELS] + [
    SOURCE_PREFIX + '_materials.urdf.xacro']


def target_paths(include_plugs=False):
    """Return the exact relative allowlist, with plugs explicitly opted in."""
    targets = [(Path(f'CameraModel/RealSense/RealSense_D{model}/meshes/glb/d{model}.glb'), 'housing')
               for model in BODY_MODELS]
    if include_plugs:
        targets.extend((Path(f'CameraModel/RealSense/RealSense_D{model}/meshes/glb/plug.glb'), 'plug')
                       for model in PLUG_MODELS)
    return targets


def housing_finish(model=None):
    """D455's broad rounded shoulder uses a softer reviewed highlight."""
    return MODEL_FINISHES.get(model, BODY_FINISH)


def _reject_extensions(value):
    if isinstance(value, dict):
        if value.get('extensions'):
            raise ValueError('Material trial does not support glTF extensions')
        for key, child in value.items():
            if key != 'extras':
                _reject_extensions(child)
    elif isinstance(value, list):
        for child in value:
            _reject_extensions(child)


def _unit_values(value, size, label):
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(f'Invalid {label}')
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
           or not 0 <= x <= 1 for x in value):
        raise ValueError(f'Invalid {label}')


def supported_input(tree):
    """Reject appearance inputs this scalar-only trial cannot faithfully tune."""
    if any(tree.get(key) for key in ('textures', 'images', 'samplers')):
        raise ValueError('Material trial does not support textures')
    if any(tree.get(key) for key in ('skins', 'animations', 'extensionsRequired', 'extensionsUsed')):
        raise ValueError('Material trial does not support skins, animations or extensions')
    _reject_extensions(tree)
    for material in tree.get('materials', []):
        if not isinstance(material, dict):
            raise ValueError('Invalid material')
        if material.get('alphaMode', 'OPAQUE') != 'OPAQUE':
            raise ValueError('Material trial requires opaque materials')
        pbr = material.get('pbrMetallicRoughness', {})
        if not isinstance(pbr, dict):
            raise ValueError('Invalid PBR material')
        if any('texture' in key.lower() for key in (*material, *pbr)):
            raise ValueError('Material trial does not support textures')
        _unit_values(pbr.get('baseColorFactor', [1, 1, 1, 1]), 4, 'material base color')
        _unit_values([pbr.get('metallicFactor', 1), pbr.get('roughnessFactor', 1)], 2, 'material finish')
        if 'extras' in material and not isinstance(material['extras'], dict):
            raise ValueError('Material extras must be an object to preserve metadata')
    triangles = []
    for mi, mesh in enumerate(tree.get('meshes', [])):
        if 'weights' in mesh:
            raise ValueError('Material trial does not support morph weights')
        for pi, primitive in enumerate(mesh['primitives']):
            if primitive.get('targets'):
                raise ValueError('Material trial does not support morph targets')
            if set(primitive['attributes']) - {'POSITION', 'NORMAL'}:
                raise ValueError('Material trial only supports POSITION and NORMAL attributes')
            if primitive.get('mode', 4) not in (1, 4):
                raise ValueError('Material trial only supports triangles and optional CAD lines')
            index = primitive.get('material')
            if index is not None and (type(index) is not int or not 0 <= index < len(tree.get('materials', []))):
                raise ValueError('Primitive references an invalid material')
            if primitive.get('mode', 4) == 4:
                triangles.append((mi, pi))
    if not triangles:
        raise ValueError('Camera visual has no triangles')
    return triangles


def plug_material():
    return {'name': 'Camera USB plug graphite polymer', 'pbrMetallicRoughness': {
        'baseColorFactor': srgb('262B30'), 'metallicFactor': 0.0, 'roughnessFactor': .55},
        'extras': {MARKER: {'profile': PROFILE, 'role': 'plug_polymer'}}}


def refine_camera(tree, kind, model=None):
    """Plan scalar finish edits or missing plug bindings without editing input."""
    if kind not in ('housing', 'plug'):
        raise ValueError(f'Unknown camera target role: {kind}')
    triangles = supported_input(tree)
    target, changes = copy.deepcopy(tree), []
    if kind == 'housing':
        finish = housing_finish(model)
        used = set()
        for mi, pi in triangles:
            index = tree['meshes'][mi]['primitives'][pi].get('material')
            if index is None:
                raise ValueError('Camera housing triangle is missing an existing material')
            used.add(index)
        for index in sorted(used):
            material = target['materials'][index]
            before = copy.deepcopy(material)
            material.setdefault('pbrMetallicRoughness', {}).update(finish)
            material.setdefault('extras', {})[MARKER] = {'profile': PROFILE, 'role': 'satin_housing'}
            if material != before:
                changes.append({'kind': 'finish', 'material': index, 'before': before,
                                'after': copy.deepcopy(material)})
    else:
        missing = [(mi, pi) for mi, pi in triangles if 'material' not in tree['meshes'][mi]['primitives'][pi]]
        if missing:
            material = plug_material()
            # Reuse only this exact trial material, never a similarly named one.
            existing = target.get('materials', [])
            index = next((i for i, value in enumerate(existing) if value == material), len(existing))
            if index == len(existing):
                target.setdefault('materials', []).append(material)
                changes.append({'kind': 'added_material', 'material': index, 'after': material})
            for mi, pi in missing:
                target['meshes'][mi]['primitives'][pi]['material'] = index
                changes.append({'kind': 'added_binding', 'mesh': mi, 'primitive': pi, 'material': index})
    return target, changes


def assert_allowed_changes(before, after, kind, original_bin, new_bin, model=None):
    """Independently remove only permitted edits, then compare the complete JSON."""
    if original_bin != new_bin:
        raise ValueError('Embedded BIN changed')
    restored = copy.deepcopy(after)
    old_materials = before.get('materials', [])
    new_materials = restored.get('materials', [])
    if kind == 'housing':
        finish = housing_finish(model)
        if len(old_materials) != len(new_materials):
            raise ValueError('Housing material count changed')
        used = {p['material'] for m in before['meshes'] for p in m['primitives']
                if p.get('mode', 4) == 4}
        for index in used:
            old, new = old_materials[index], new_materials[index]
            if any(new.get('pbrMetallicRoughness', {}).get(k) != v for k, v in finish.items()):
                raise ValueError('Unexpected camera housing finish')
            if new.get('extras', {}).get(MARKER) != {'profile': PROFILE, 'role': 'satin_housing'}:
                raise ValueError('Unexpected camera housing metadata')
            for key in finish:
                if key in old.get('pbrMetallicRoughness', {}):
                    new['pbrMetallicRoughness'][key] = old['pbrMetallicRoughness'][key]
                else:
                    new['pbrMetallicRoughness'].pop(key)
            if 'pbrMetallicRoughness' not in old and not new['pbrMetallicRoughness']:
                new.pop('pbrMetallicRoughness')
            if MARKER in old.get('extras', {}):
                new['extras'][MARKER] = copy.deepcopy(old['extras'][MARKER])
            else:
                new['extras'].pop(MARKER)
            if 'extras' not in old and not new['extras']:
                new.pop('extras')
    elif kind == 'plug':
        count = len(old_materials)
        if len(new_materials) not in (count, count + 1) or new_materials[:count] != old_materials:
            raise ValueError('Existing plug material changed')
        if len(new_materials) == count + 1 and new_materials[-1] != plug_material():
            raise ValueError('Unexpected added plug material')
        missing = [(mi, pi) for mi, m in enumerate(before['meshes']) for pi, p in enumerate(m['primitives'])
                   if p.get('mode', 4) == 4 and 'material' not in p]
        for mi, pi in missing:
            primitive = restored['meshes'][mi]['primitives'][pi]
            index = primitive.pop('material', None)
            if type(index) is not int or not 0 <= index < len(new_materials) or new_materials[index] != plug_material():
                raise ValueError('Unexpected new plug material binding')
        if 'materials' in before:
            restored['materials'] = copy.deepcopy(old_materials)
        else:
            restored.pop('materials', None)
    else:
        raise ValueError(f'Unknown camera target role: {kind}')
    if restored != before:
        raise ValueError('Non-allowed camera JSON changed')


def _backup_location(root, backup_dir):
    if backup_dir is None:
        raise ValueError('--apply requires --backup-dir')
    backup = Path(backup_dir).resolve()
    if backup.is_relative_to(root) or root.is_relative_to(backup):
        raise ValueError('Backup must be outside the asset root')
    return backup


def report_destination(root, report):
    """Keep reports outside every asset category, following symlink targets."""
    root, report = Path(root).resolve(), Path(report)
    destination = report.resolve()
    if report.suffix.lower() != '.json' or destination.suffix.lower() != '.json':
        raise ValueError('Report path must resolve to a .json file')
    for category in ('CameraModel', 'RobotModel', 'ToolModel'):
        if destination.is_relative_to((root / category).resolve()):
            raise ValueError('Report must be outside CameraModel, RobotModel and ToolModel')
    return destination


def preflight_report(root, report):
    """Check report writes before apply without changing an existing report."""
    destination = report_destination(root, report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        # No O_TRUNC or writes: retain report bytes/mtime. NONBLOCK prevents a
        # FIFO from hanging and NOFOLLOW rejects a new symlink after resolve.
        descriptor = os.open(destination, os.O_WRONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError('Existing report must be a regular writable file')
        finally:
            os.close(descriptor)
    with tempfile.NamedTemporaryFile(prefix='.' + destination.name + '.preflight-', dir=destination.parent):
        pass
    return destination


def _stage(path, data):
    """Create a complete replacement alongside an existing asset."""
    handle = tempfile.NamedTemporaryFile(prefix=f'.{path.name}.', dir=path.parent, delete=False)
    staged = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(stat.S_IMODE(path.stat().st_mode))
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def run(root, *, include_plugs=False, apply=False, backup_dir=None):
    """Preflight every input and backup before writing any visual resource."""
    root = Path(root).resolve()
    backup = _backup_location(root, backup_dir) if apply else None
    records, plans = [], []
    for relative, kind in target_paths(include_plugs):
        path = root / relative
        model = relative.parts[2].removeprefix('RealSense_')
        if path.resolve() != path or not path.is_file():
            raise ValueError(f'Expected regular camera target without symlinks: {relative}')
        old = path.read_bytes()
        validate_glb(old)
        original, binary = read_glb(old)
        target, changes = refine_camera(original, kind, model=model)
        new = encode_glb(target, binary) if changes else old
        actual, actual_bin = read_glb(new)
        assert_allowed_changes(original, actual, kind, binary, actual_bin, model=model)
        info = validate_glb(new)
        copy_path = backup / relative if backup else None
        if changes and apply:
            if copy_path.resolve() != copy_path or not copy_path.resolve().is_relative_to(backup):
                raise ValueError(f'Backup target has a symlink: {copy_path}')
            if copy_path.exists() and (not copy_path.is_file() or copy_path.read_bytes() != old):
                raise ValueError(f'Backup already has different content: {copy_path}')
            plans.append((path, copy_path, old, new))
        records.append({'path': relative.as_posix(), 'role': kind,
                        'status': ('applied' if apply else 'proposed') if changes else 'unchanged',
                        'before_sha256': hashlib.sha256(old).hexdigest(),
                        'after_sha256': hashlib.sha256(new).hexdigest(),
                        'bin_sha256': hashlib.sha256(binary).hexdigest(), 'bin_unchanged': True,
                        'only_allowed_json_changed': True, 'triangles': info['faces'], 'changes': changes})
    if apply:
        # Check all source files before even preparing backups. Back up the
        # complete plan before any replacement, so late backup errors are safe.
        for path, _, old, _ in plans:
            if path.read_bytes() != old:
                raise ValueError(f'Camera changed during preflight: {path}')
        for _, copy_path, old, _ in plans:
            copy_path.parent.mkdir(parents=True, exist_ok=True)
            if not copy_path.exists():
                with copy_path.open('xb') as handle:
                    handle.write(old)
        staged = []
        try:
            for path, _, _, new in plans:
                staged.append((path, _stage(path, new)))
            for path, _, old, _ in plans:
                if path.read_bytes() != old:
                    raise ValueError(f'Camera changed during staging: {path}')
            for path, replacement in staged:
                os.replace(replacement, path)
        finally:
            for _, replacement in staged:
                replacement.unlink(missing_ok=True)
    summary = {'mode': 'apply' if apply else 'dry-run', 'profile': PROFILE,
               'visual_glbs': len(records), 'changed_glbs': sum(bool(x['changes']) for x in records),
               'include_plugs': include_plugs, 'all_bin_unchanged': True, 'only_allowed_json_changed': True,
               'collision_assets_in_scope': False}
    return {'summary': summary, 'generated_at': datetime.now(timezone.utc).isoformat(),
            'finish_note': 'Visualization approximations; source housing colors retained. '
                           'No measured manufacturer finish or missing optical detail is claimed.',
            'source_urls': SOURCE_URLS, 'files': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--include-plugs', action='store_true', help='Opt in to the graphite USB plug appearance trial')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.apply and args.backup_dir is None:
        parser.error('--apply requires --backup-dir')
    try:
        report_path = preflight_report(args.root, args.report)
        report = run(args.root, include_plugs=args.include_plugs, apply=args.apply, backup_dir=args.backup_dir)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()
