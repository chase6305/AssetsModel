"""Camera appearance edits must remain selective, reversible and geometry-free."""
import copy
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from glb_format import read_glb
from refine_camera_materials import (assert_allowed_changes, plug_material, refine_camera,
                                     report_destination, run, target_paths)
from refine_materials import encode_glb


def sample_body():
    binary = struct.pack('<9f3H2x', 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 2)
    tree = {'asset': {'version': '2.0', 'extras': {'units': 'meters'}}, 'scene': 0,
            'scenes': [{'nodes': [0]}], 'nodes': [{'mesh': 0, 'translation': [.2, .3, .4]}],
            'materials': [{'name': 'URDF color', 'pbrMetallicRoughness': {
                'baseColorFactor': [.5, .5, .5, 1], 'metallicFactor': 0, 'roughnessFactor': 1},
                'doubleSided': False, 'extras': {'retain': {'cad': 'source'}}}],
            'meshes': [{'name': 'camera', 'primitives': [{'attributes': {'POSITION': 0},
                         'indices': 1, 'material': 0}]}], 'buffers': [{'byteLength': len(binary)}],
            'bufferViews': [{'buffer': 0, 'byteLength': 36}, {'buffer': 0, 'byteOffset': 36, 'byteLength': 6}],
            'accessors': [{'bufferView': 0, 'componentType': 5126, 'type': 'VEC3', 'count': 3},
                          {'bufferView': 1, 'componentType': 5123, 'type': 'SCALAR', 'count': 3}]}
    return tree, binary


def sample_plug():
    tree, binary = sample_body()
    tree.pop('materials')
    tree['meshes'][0]['primitives'][0].pop('material')
    return tree, binary


def fixture(root):
    originals = {}
    for relative, kind in target_paths(True):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        tree, binary = sample_body() if kind == 'housing' else sample_plug()
        originals[relative] = encode_glb(tree, binary)
        path.write_bytes(originals[relative])
    # A collision file adjacent to an eligible plug must never be considered.
    collision = root / target_paths(True)[-1][0].with_name('plug_collision.glb')
    collision.write_bytes(b'collision sentinel, intentionally not a GLB')
    return originals, collision


class CameraMaterialTests(unittest.TestCase):
    def test_body_preserves_all_non_finish_fields_and_binary(self):
        tree, binary = sample_body()
        source = copy.deepcopy(tree)
        target, edits = refine_camera(tree, 'housing')
        parsed, parsed_bin = read_glb(encode_glb(target, binary))
        assert_allowed_changes(tree, parsed, 'housing', binary, parsed_bin)
        self.assertTrue(edits)
        self.assertEqual(tree, source)
        self.assertEqual(parsed_bin, binary)
        self.assertEqual(parsed['meshes'], tree['meshes'])
        self.assertEqual(parsed['nodes'], tree['nodes'])
        self.assertEqual(parsed['accessors'], tree['accessors'])
        self.assertEqual(parsed['materials'][0]['pbrMetallicRoughness']['baseColorFactor'], [.5, .5, .5, 1])
        self.assertEqual(parsed['materials'][0]['extras']['retain'], {'cad': 'source'})
        self.assertEqual(parsed['materials'][0]['name'], 'URDF color')
        self.assertEqual(parsed['materials'][0]['doubleSided'], False)

    def test_finish_is_idempotent_and_missing_body_material_fails(self):
        tree, _ = sample_body()
        first, _ = refine_camera(tree, 'housing')
        second, edits = refine_camera(first, 'housing')
        self.assertEqual(first, second)
        self.assertEqual(edits, [])
        tree['meshes'][0]['primitives'][0].pop('material')
        with self.assertRaisesRegex(ValueError, 'missing an existing material'):
            refine_camera(tree, 'housing')

    def test_plug_embeds_linear_graphite_and_does_not_touch_authored_material(self):
        tree, binary = sample_plug()
        tree['materials'] = [{'name': 'authored label', 'pbrMetallicRoughness': {'baseColorFactor': [1, .2, .1, 1]}}]
        tree['meshes'][0]['primitives'].append({'attributes': {'POSITION': 0}, 'indices': 1, 'material': 0})
        target, edits = refine_camera(tree, 'plug')
        assert_allowed_changes(tree, target, 'plug', binary, binary)
        self.assertEqual(target['materials'][0], tree['materials'][0])
        self.assertEqual(target['meshes'][0]['primitives'][1], tree['meshes'][0]['primitives'][1])
        self.assertEqual(target['meshes'][0]['primitives'][0]['material'], 1)
        self.assertEqual(target['materials'][1], plug_material())
        color = target['materials'][1]['pbrMetallicRoughness']['baseColorFactor']
        self.assertAlmostEqual(color[0], .01938236, places=7)
        self.assertTrue(edits)
        again, edits = refine_camera(target, 'plug')
        self.assertEqual(again, target)
        self.assertEqual(edits, [])
        authored, _ = sample_body()
        untouched, edits = refine_camera(authored, 'plug')
        self.assertEqual(untouched, authored)
        self.assertEqual(edits, [])

    def test_geometry_and_unrelated_json_edits_are_rejected(self):
        tree, binary = sample_body()
        target, _ = refine_camera(tree, 'housing')
        for mutate in (lambda t: t['nodes'][0]['translation'].__setitem__(0, .9),
                       lambda t: t['materials'][0]['pbrMetallicRoughness']['baseColorFactor'].__setitem__(0, .4),
                       lambda t: t['meshes'][0]['primitives'][0].__setitem__('indices', 0)):
            changed = copy.deepcopy(target)
            mutate(changed)
            with self.assertRaisesRegex(ValueError, 'Non-allowed'):
                assert_allowed_changes(tree, changed, 'housing', binary, binary)
        with self.assertRaisesRegex(ValueError, 'BIN changed'):
            assert_allowed_changes(tree, target, 'housing', binary, binary[:-1] + b'X')
        plug, _ = sample_plug()
        changed, _ = refine_camera(plug, 'plug')
        changed['meshes'][0]['primitives'][0]['attributes']['POSITION'] = 99
        with self.assertRaisesRegex(ValueError, 'Non-allowed'):
            assert_allowed_changes(plug, changed, 'plug', binary, binary)

    def test_texture_and_unsupported_inputs_fail_explicitly(self):
        tree, _ = sample_body()
        changes = [({'textures': [{'source': 0}]}, 'textures'),
                   ({'animations': [{'channels': []}]}, 'animations'),
                   ({'skins': [{'joints': [0]}]}, 'skins'),
                   ({'extensionsUsed': ['KHR_materials_unlit']}, 'extensions')]
        for update, message in changes:
            candidate = copy.deepcopy(tree)
            candidate.update(update)
            with self.subTest(update=update), self.assertRaisesRegex(ValueError, message):
                refine_camera(candidate, 'housing')
        tree['materials'][0]['pbrMetallicRoughness']['baseColorTexture'] = {'index': 0}
        with self.assertRaisesRegex(ValueError, 'textures'):
            refine_camera(tree, 'housing')
        tree, _ = sample_body()
        tree['meshes'][0]['primitives'][0]['attributes']['COLOR_0'] = 0
        with self.assertRaisesRegex(ValueError, 'attributes'):
            refine_camera(tree, 'housing')

    def test_allowlist_has_six_bodies_and_four_opt_in_plugs_no_collision(self):
        self.assertEqual(len(target_paths()), 6)
        self.assertEqual(len(target_paths(True)), 10)
        for path, role in target_paths(True):
            self.assertEqual(path.parts[:2], ('CameraModel', 'RealSense'))
            self.assertNotIn('collision', path.name)
            self.assertEqual(role == 'plug', path.name == 'plug.glb')

    def test_reports_cannot_overwrite_off_scope_assets_or_symlink_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'assets'
            root.mkdir()
            for category in ('CameraModel', 'RobotModel', 'ToolModel'):
                destination = root / category / 'model' / 'provenance.json'
                destination.parent.mkdir(parents=True)
                destination.write_text('{"original":true}')
                with self.subTest(category=category), self.assertRaisesRegex(ValueError, 'outside CameraModel'):
                    report_destination(root, destination)
                alias = root / f'{category}-report.json'
                alias.symlink_to(destination)
                with self.assertRaisesRegex(ValueError, 'outside CameraModel'):
                    report_destination(root, alias)
                self.assertEqual(destination.read_text(), '{"original":true}')
            docs = root / 'docs' / 'quality' / 'camera-materials.json'
            external = Path(directory) / 'camera-report.json'
            self.assertEqual(report_destination(root, docs), docs)
            self.assertEqual(report_destination(root, external), external)
            alias_dir = root / 'docs-alias'
            alias_dir.symlink_to(root / 'CameraModel', target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'outside CameraModel'):
                report_destination(root, alias_dir / 'new-report.json')

    def test_apply_requires_external_backup_and_dryrun_writes_no_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals, collision = fixture(root)
            report = run(root, include_plugs=True)
            self.assertEqual(report['summary']['changed_glbs'], 10)
            for path, old in originals.items():
                self.assertEqual((root / path).read_bytes(), old)
            self.assertEqual(collision.read_bytes(), b'collision sentinel, intentionally not a GLB')
            with self.assertRaisesRegex(ValueError, 'requires --backup-dir'):
                run(root, apply=True)
            with self.assertRaisesRegex(ValueError, 'outside the asset root'):
                run(root, apply=True, backup_dir=root / 'backup')

    def test_late_backup_conflict_prevents_every_asset_write(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, backup = base / 'assets', base / 'backup'
            originals, _ = fixture(root)
            conflicting = backup / target_paths(True)[-1][0]
            conflicting.parent.mkdir(parents=True)
            conflicting.write_bytes(b'previous unrelated backup')
            with self.assertRaisesRegex(ValueError, 'different content'):
                run(root, apply=True, include_plugs=True, backup_dir=backup)
            for path, old in originals.items():
                self.assertEqual((root / path).read_bytes(), old)
            self.assertEqual(conflicting.read_bytes(), b'previous unrelated backup')

    def test_apply_backs_up_every_change_and_repeat_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, backup = base / 'assets', base / 'backup'
            originals, collision = fixture(root)
            report = run(root, apply=True, include_plugs=True, backup_dir=backup)
            self.assertEqual(report['summary']['changed_glbs'], 10)
            for path, old in originals.items():
                self.assertEqual((backup / path).read_bytes(), old)
                self.assertNotEqual((root / path).read_bytes(), old)
                record = next(x for x in report['files'] if x['path'] == path.as_posix())
                self.assertTrue(record['bin_unchanged'])
                self.assertNotEqual(record['before_sha256'], record['after_sha256'])
            self.assertEqual(collision.read_bytes(), b'collision sentinel, intentionally not a GLB')
            mtimes = {p: (root / p).stat().st_mtime_ns for p in originals}
            repeat = run(root, apply=True, include_plugs=True, backup_dir=backup)
            self.assertEqual(repeat['summary']['changed_glbs'], 0)
            self.assertEqual(mtimes, {p: (root / p).stat().st_mtime_ns for p in originals})


if __name__ == '__main__':
    unittest.main()
