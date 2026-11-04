"""Import four pinned upstream robot descriptions into portable URDF + GLB models.

Inputs are read-only Git checkouts. Mesh conversion uses the repository's lossless
corner-preserving converter, without geometry scaling or decimation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from convert_glb import GLBWriter, compact, convert, dae_parts, smooth_stl_normals
from glb_format import read_glb, validate_glb, accessor_bytes
from validate_assets import validate_urdf

PINNED = {
    'xarm': {'url': 'https://github.com/xArm-Developer/xarm_ros',
             'commit': 'cf51617d98bc55a88d9aa735acfa43a0bbb8e425', 'license': 'BSD-3-Clause', 'package': 'xarm_description'},
    'iiwa': {'url': 'https://github.com/IFL-CAMP/iiwa_stack',
             'commit': '44f9d13c1b444d5dc9fd3e43ba60b7d3b2ea2bbb', 'license': 'BSD-2-Clause', 'package': 'iiwa_description'},
    'kortex': {'url': 'https://github.com/Kinovarobotics/ros_kortex',
               'commit': 'c3280d8a6a6b5d96590d51538e3f265710a6c77d', 'license': 'BSD-3-Clause', 'package': 'kortex_description'},
}
MODELS = {
    'xArm6': {'source': 'xarm', 'vendor': 'UFACTORY', 'input': 'urdf/xarm_device.urdf.xacro',
              'args': {'dof': '6', 'robot_type': 'xarm', 'add_gripper': 'false', 'mesh_suffix': 'dae'}, 'robot_name': 'UFACTORY_xArm6'},
    'Lite6': {'source': 'xarm', 'vendor': 'UFACTORY', 'input': 'urdf/xarm_device.urdf.xacro',
             'args': {'dof': '6', 'robot_type': 'lite', 'add_gripper': 'false', 'mesh_suffix': 'dae'}, 'robot_name': 'UFACTORY_Lite6'},
    'iiwa14': {'source': 'iiwa', 'vendor': 'KUKA', 'input': 'urdf/iiwa14.urdf.xacro',
               'args': {}, 'robot_name': 'KUKA_LBR_iiwa14_R820'},
    'Gen3': {'source': 'kortex', 'vendor': 'Kinova',
             'input': 'arms/gen3/7dof/urdf/GEN3-7DOF-NOVISION_FOR_URDF_ARM_V12.urdf',
             'args': {}, 'robot_name': 'Kinova_Gen3_7DOF'},
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def serialise_glb(tree, binary):
    import struct
    raw = json.dumps(tree, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
    raw += b' ' * (-len(raw) % 4)
    binary += b'\0' * (-len(binary) % 4)
    return (struct.pack('<III', 0x46546C67, 2, 28 + len(raw) + len(binary))
            + struct.pack('<II', len(raw), 0x4E4F534A) + raw
            + struct.pack('<II', len(binary), 0x004E4942) + binary)


def srgb_to_linear(color):
    rgb = np.asarray(color, dtype=float).copy()
    rgb[:3] = np.where(rgb[:3] <= 0.04045, rgb[:3] / 12.92, ((rgb[:3] + 0.055) / 1.055) ** 2.4)
    return rgb.tolist()


def convert_source(path, collision):
    if collision or path.suffix.lower() != '.dae':
        return convert(path, collision)
    writer = GLBWriter(path.name)
    expected = []
    smoothed = 0
    for name, vertices, indices, normals, material, mode in dae_parts(path, False):
        if mode == 4:
            corners = np.asarray(normals).reshape(-1, 3, 3)
            flat = np.max(np.linalg.norm(corners[:, :1] - corners, axis=2)) < 1e-6
            if flat:
                normals = smooth_stl_normals(vertices, indices)
                smoothed += len(indices)
        v, f, n = compact(vertices, indices, normals)
        primitive = writer.add(name, v, f, n, material, mode)
        expected.append((primitive, np.asarray(vertices)[indices].reshape(-1, 3)))
    payload = writer.export()
    tree, binary = read_glb(payload)
    max_error = 0.0
    for primitive, corners in expected:
        values, _, _ = accessor_bytes(tree, binary, primitive['attributes']['POSITION'])
        vertices = np.frombuffer(values, dtype='<f4').reshape(-1, 3)
        values, component, _ = accessor_bytes(tree, binary, primitive['indices'])
        indices = np.frombuffer(values, dtype={'H': '<u2', 'I': '<u4'}[component])
        error = float(np.linalg.norm(vertices[indices] - corners, axis=1).max())
        if error > max(1e-7, float(np.max(np.abs(corners))) * 2e-7):
            raise ValueError('Visual conversion moved source corners')
        max_error = max(max_error, error)
    return payload, {**validate_glb(payload), 'max_corner_error_m': max_error, 'collision': False,
                     'normal_policy': 'source_smooth_normals_or_30_degree_local_fan_for_flat_CAD',
                     'smoothed_source_flat_faces': smoothed}


def pbr_materials(payload, model, urdf_color=None):
    tree, binary = read_glb(payload)
    materials = tree.get('materials', [])
    if not materials:
        materials = [{'name': 'Source URDF surface', 'pbrMetallicRoughness': {
            'baseColorFactor': srgb_to_linear(urdf_color or [0.55, 0.57, 0.59, 1]), 'metallicFactor': 0.0, 'roughnessFactor': 0.36}}]
        tree['materials'] = materials
        for mesh in tree['meshes']:
            for primitive in mesh['primitives']:
                primitive['material'] = 0
    for material in materials:
        pbr = material.setdefault('pbrMetallicRoughness', {})
        rgb = np.asarray(pbr.get('baseColorFactor', [1, 1, 1, 1]), dtype=float)
        label = material.get('name', '')
        if model in ('xArm6', 'Lite6'):
            # Source CAD has white shells and grey joint covers; preserve grouping.
            if min(rgb[:3]) > 0.8:
                rgb[:3] = 0.72
                pbr.update(metallicFactor=0.0, roughnessFactor=0.30)
            else:
                rgb[:3] *= 0.48
                pbr.update(metallicFactor=0.25 if '金属' in label else 0.0, roughnessFactor=0.40)
        elif model == 'iiwa14':
            # Keep the upstream orange/grey segmentation, using a satin paint response.
            pbr.update(metallicFactor=0.0, roughnessFactor=0.32 if rgb[0] > rgb[2] * 1.5 else 0.42)
        else:
            # Gen3 source URDF provides the grey housing; STL itself has no color.
            pbr.update(metallicFactor=0.0, roughnessFactor=0.38)
        pbr['baseColorFactor'] = rgb.tolist()
        material['doubleSided'] = False
    return serialise_glb(tree, binary)


def expand(source, package, input_path, mappings, work):
    if not input_path.endswith('.xacro'):
        return ET.parse(source / package / input_path).getroot()
    import xacro
    stage = work / package
    shutil.copytree(source / package / 'urdf', stage / 'urdf')
    if (source / package / 'config').exists():
        shutil.copytree(source / package / 'config', stage / 'config')
    for path in stage.rglob('*.xacro'):
        path.write_text(path.read_text().replace('$(find ' + package + ')', str(stage)))
    document = xacro.process_file(str(stage / input_path), mappings=mappings)
    return ET.fromstring(document.toxml())


def verify_source_tree(source, upstream, exported=False):
    if exported:
        listing = json.loads((source / '.assetsmodel-source.json').read_text())
        commit, files = listing['commit'], listing['files']
    else:
        commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        rows = subprocess.check_output(['git', '-C', str(source), 'ls-tree', '-r', 'HEAD'], text=True).splitlines()
        files = {path: info.split()[-1] for info, path in (row.split('\t') for row in rows) if info.split()[1] == 'blob'}
    if commit != upstream['commit']:
        raise ValueError(f'Expected pinned {upstream["commit"]}; source is {commit}')
    checked = 0
    for relative, oid in files.items():
        if relative != 'LICENSE' and not relative.startswith(upstream['package'] + '/'):
            continue
        path = source / relative
        if path.is_file():
            data = path.read_bytes()
            actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            if actual != oid:
                raise ValueError(f'Source differs from pinned tree: {relative}')
            checked += 1
    if not checked:
        raise ValueError('No pinned source files found')
    return {'method': 'Git blob SHA-1 against pinned tree', 'verified_files': checked, 'commit': commit}


def _build_model(name, source, output, allow_export=False):
    config = MODELS[name]
    upstream = PINNED[config['source']]
    source_verification = verify_source_tree(source, upstream, allow_export)
    target = output / 'RobotModel' / config['vendor'] / name
    if target.exists() and any(target.iterdir()):
        raise ValueError(f'Refusing to overwrite model directory: {target}')
    target.mkdir(parents=True, exist_ok=True)
    records = []
    with tempfile.TemporaryDirectory(prefix='assetsmodel-import-') as temp:
        robot = expand(source, upstream['package'], config['input'], config['args'], Path(temp))
        robot.set('name', config['robot_name'])
        omitted_inertias = []
        omitted_fixed_axes = []
        for joint in robot.findall('joint'):
            axis = joint.find('axis')
            if joint.get('type') == 'fixed' and axis is not None and all(float(v) == 0 for v in axis.get('xyz').split()):
                omitted_fixed_axes.append({'joint': joint.get('name'), 'source_xml': ET.tostring(axis, encoding='unicode'), 'reason': 'zero axis is unused for a fixed joint'})
                joint.remove(axis)
        for link in robot.findall('link'):
            for extra in link.findall('self_collision_checking'):
                link.remove(extra)
            inertial = link.find('inertial')
            if inertial is not None:
                inertia = inertial.find('inertia')
                a = {k: float(v) for k, v in inertia.attrib.items()}
                matrix = np.array([[a['ixx'], a['ixy'], a['ixz']], [a['ixy'], a['iyy'], a['iyz']], [a['ixz'], a['iyz'], a['izz']]])
                if np.linalg.eigvalsh(matrix).min() <= 0:
                    omitted_inertias.append({'link': link.get('name'), 'source_xml': ET.tostring(inertial, encoding='unicode'), 'reason': 'source inertia is not positive definite; no replacement fabricated'})
                    link.remove(inertial)
        # Standalone asset bundles omit ROS controller/transmission/plugin metadata.
        for element in list(robot):
            if element.tag not in ('material', 'link', 'joint'):
                robot.remove(element)
        globals_ = {m.get('name'): m for m in robot.findall('material')}
        for kind, folder in [('visual', 'Visual'), ('collision', 'Collision')]:
            converted = {}
            for element in robot.findall(f'link/{kind}'):
                mesh = element.find('geometry/mesh')
                if mesh is None:
                    continue
                original = mesh.get('filename')
                package_prefix = 'package://' + upstream['package'] + '/'
                if not original.startswith(package_prefix):
                    raise ValueError(f'Unexpected resource URI: {original}')
                relative = original[len(package_prefix):]
                incoming = source / upstream['package'] / relative
                stem = incoming.stem
                outfile = Path(folder) / 'glb' / (stem + '.glb')
                if relative not in converted:
                    payload, stats = convert_source(incoming, kind == 'collision')
                    if kind == 'visual':
                        material = element.find('material')
                        color = None
                        if material is not None:
                            rgba = material.find('color')
                            if rgba is None and material.get('name') in globals_:
                                rgba = globals_[material.get('name')].find('color')
                            if rgba is not None:
                                color = [float(x) for x in rgba.get('rgba').split()]
                        payload = pbr_materials(payload, name, color)
                    stats.update(validate_glb(payload))
                    destination = target / outfile
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(payload)
                    records.append({'source': relative, 'source_sha256': sha(incoming), 'output': outfile.as_posix(),
                                    'output_sha256': sha(destination), **stats})
                    converted[relative] = outfile
                mesh.set('filename', converted[relative].as_posix())
        ET.indent(robot, space='  ')
        urdf = target / (name + '.urdf')
        ET.ElementTree(robot).write(urdf, encoding='utf-8', xml_declaration=True)
    shutil.copyfile(source / 'LICENSE', target / 'LICENSE')
    notice = (f"{name}: derived from {upstream['url']} at commit {upstream['commit']}.\n"
              f"Upstream license: {upstream['license']}; full text is in LICENSE.\n"
              f"Input description: {upstream['package']}/{config['input']}.\n"
              'Changes: expanded xacro; omitted ROS plugins/controllers/transmissions; converted referenced meshes\n'
              'to embedded GLB; localized resource paths; adjusted visual PBR factors. Link frames, joint axes,\n'
              'limits, valid inertia, source surface geometry and mesh origin/scale are preserved.\n'
              'Any omitted invalid source inertia or unused zero axis on a fixed joint is documented verbatim in provenance.json.\n'
              'Brand appearance uses the source CAD material groups. Added PBR factors are visual approximations.\n')
    (target / 'NOTICE').write_text(notice)
    report = {'model': name, 'upstream': upstream, 'xacro_arguments': config['args'],
              'urdf': str(urdf.relative_to(output)), 'meshes': records, 'omitted_invalid_inertias': omitted_inertias, 'omitted_unused_fixed_axes': omitted_fixed_axes, 'source_verification': source_verification}
    (target / 'provenance.json').write_text(json.dumps(report, indent=2) + '\n')
    result = validate_urdf(urdf, glb_only=True)
    if result['errors']:
        raise ValueError(result['errors'])
    print(json.dumps({'model': name, 'meshes': len(records), 'validation': result}), flush=True)
    return report


def import_model(name, source, output, allow_export=False):
    """Publish only a completely built and validated model directory."""
    config = MODELS[name]
    final = output / 'RobotModel' / config['vendor'] / name
    if final.exists() and any(final.iterdir()):
        raise ValueError(f'Refusing to overwrite model directory: {final}')
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='assetsmodel-build-', dir=final.parent) as temp:
        staging = Path(temp)
        report = _build_model(name, source, staging, allow_export)
        built = staging / 'RobotModel' / config['vendor'] / name
        if final.exists():
            final.rmdir()
        built.replace(final)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xarm', type=Path)
    parser.add_argument('--iiwa', type=Path)
    parser.add_argument('--kortex', type=Path)
    parser.add_argument('--models', nargs='+', choices=MODELS, default=list(MODELS))
    parser.add_argument('--output', type=Path, required=True, help='Asset root; selected model directories must be empty')
    parser.add_argument('--exported-sources', action='store_true', help='Inputs include .assetsmodel-source.json with the pinned Git tree blob list')
    args = parser.parse_args()
    for model in args.models:
        source = getattr(args, MODELS[model]['source'])
        if source is None:
            parser.error(f'Missing --{MODELS[model]["source"]}')
        import_model(model, source.resolve(), args.output.resolve(), args.exported_sources)


if __name__ == '__main__':
    main()
