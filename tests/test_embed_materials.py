import copy
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from embed_missing_materials import collect_references, embed, assert_only_added_material_bindings
from refine_materials import encode_glb


def sample_glb():
    binary = struct.pack('<9f3H2x', 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 2)
    tree = {'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{'nodes': [0]}], 'nodes': [{'mesh': 0}],
            'meshes': [{'primitives': [{'attributes': {'POSITION': 0}, 'indices': 1}]}],
            'buffers': [{'byteLength': len(binary)}],
            'bufferViews': [{'buffer': 0, 'byteLength': 36}, {'buffer': 0, 'byteOffset': 36, 'byteLength': 6}],
            'accessors': [{'bufferView': 0, 'componentType': 5126, 'type': 'VEC3', 'count': 3},
                          {'bufferView': 1, 'componentType': 5123, 'type': 'SCALAR', 'count': 3}]}
    return tree, binary


class EmbedMaterialTests(unittest.TestCase):
    def fixture(self, folder, second_color):
        model = folder / 'RobotModel/ABB/Arm'
        glb = model / 'Visual/glb/part.glb'
        glb.parent.mkdir(parents=True)
        tree, binary = sample_glb()
        glb.write_bytes(encode_glb(tree, binary))
        for name, color in [('a', '.9 .9 .8 1'), ('b', second_color)]:
            (model / (name + '.urdf')).write_text(f'''<robot name="arm"><material name="ABBWhite"><color rgba="{color}"/></material><link name="base"><visual><geometry><mesh filename="Visual/glb/part.glb"/></geometry><material name="ABBWhite"/></visual></link></robot>''')
        return glb

    def test_conflicting_variant_colors_are_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            glb = self.fixture(root, '.1 .2 .8 1')
            original = glb.read_bytes()
            with self.assertRaisesRegex(ValueError, 'Conflicting URDF colors'):
                collect_references(root, ['ABB'])
            self.assertEqual(glb.read_bytes(), original)

    def test_named_global_color_resolves_across_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            glb = self.fixture(root, '.9 .9 .8 1')
            references = collect_references(root, ['ABB'])
            self.assertEqual(len(references[glb]), 2)
            self.assertEqual(references[glb][0]['rgba'], [.9, .9, .8, 1])

    def test_adds_only_missing_triangle_binding_and_keeps_existing_material(self):
        tree, binary = sample_glb()
        tree['materials'] = [{'name': 'red-label'}]
        tree['meshes'][0]['primitives'].append({'attributes': {'POSITION': 0}, 'indices': 1, 'material': 0})
        refs = [{'urdf': 'arm.urdf', 'name': 'ABBWhite', 'rgba': [.9, .9, .8, 1]}]
        target, missing = embed(tree, Path('/tmp/RobotModel/ABB/Arm/Visual/glb/part.glb'), refs)
        assert_only_added_material_bindings(tree, target, missing, binary, binary)
        self.assertEqual(target['meshes'][0]['primitives'][0]['material'], 1)
        self.assertEqual(target['meshes'][0]['primitives'][1]['material'], 0)
        self.assertEqual(target['materials'][0], tree['materials'][0])
        self.assertEqual(target['materials'][1]['extras']['urdf_material_source'], refs)

    def test_guard_rejects_unrelated_primitive_edit(self):
        tree, binary = sample_glb()
        refs = [{'urdf': 'arm.urdf', 'name': 'ABBWhite', 'rgba': [.9, .9, .8, 1]}]
        target, missing = embed(tree, Path('/tmp/RobotModel/ABB/Arm/Visual/glb/part.glb'), refs)
        target['meshes'][0]['primitives'][0]['indices'] = 99
        with self.assertRaisesRegex(ValueError, 'Non-material-binding'):
            assert_only_added_material_bindings(tree, target, missing, binary, binary)


if __name__ == '__main__':
    unittest.main()
