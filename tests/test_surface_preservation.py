"""Independent preservation checks catch real surface and auxiliary-scene loss."""
import copy
import json
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from verify_surface_preservation import verify_surfaces


def encode(tree, binary):
    tree = copy.deepcopy(tree); tree['buffers'] = [{'byteLength': len(binary)}]
    text = json.dumps(tree, separators=(',', ':')).encode(); text += b' ' * (-len(text) % 4)
    data = bytes(binary) + b'\0' * (-len(binary) % 4)
    return (struct.pack('<III', 0x46546C67, 2, 28+len(text)+len(data))
            + struct.pack('<II', len(text), 0x4E4F534A) + text
            + struct.pack('<II', len(data), 0x004E4942) + data)


def fixture():
    tree = {'asset': {'version': '2.0'}, 'scene': 0,
            'scenes': [{'name': 'surface', 'nodes': [0]}, {'name': 'CAD guides', 'nodes': [0, 2]}],
            'nodes': [{'translation': [2, 3, 4], 'children': [1]},
                      {'mesh': 0, 'rotation': [0, 0, 1, 0]}, {'mesh': 1}],
            'meshes': [], 'accessors': [], 'bufferViews': [],
            'materials': [{'pbrMetallicRoughness': {'baseColorFactor': [.1, .2, .3, 1]}}]}
    binary = bytearray()
    def add(rows, code, component, kind, stride=None):
        binary.extend(b'\0' * (-len(binary) % 4)); offset = len(binary)
        for row in rows:
            raw = struct.pack('<' + code * len(row), *row); binary.extend(raw)
            if stride: binary.extend(b'\0' * (stride-len(raw)))
        view = {'buffer': 0, 'byteOffset': offset, 'byteLength': len(binary)-offset}
        if stride: view['byteStride'] = stride
        tree['bufferViews'].append(view)
        tree['accessors'].append({'bufferView': len(tree['bufferViews'])-1, 'componentType': component,
                                  'type': kind, 'count': len(rows)})
        return len(tree['accessors'])-1
    p = add([[0., -0., 0.], [1., 0., 0.], [1., 1., 0.], [0., 1., 0.]], 'f', 5126, 'VEC3')
    n = add([[0., 0., 1.]]*4, 'f', 5126, 'VEC3')
    uv = add([[0., 0.], [1., 0.], [1., 1.], [0., 1.]], 'f', 5126, 'VEC2')
    color = add([[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]], 'B', 5121, 'VEC3', stride=4)
    tree['accessors'][color]['normalized'] = True
    faces = add([[0], [1], [2], [0], [2], [3]], 'H', 5123, 'SCALAR')
    lines = add([[0], [2], [1], [3]], 'H', 5123, 'SCALAR')
    attrs = {'POSITION': p, 'NORMAL': n, 'TEXCOORD_0': uv, 'COLOR_0': color}
    tree['meshes'] = [{'primitives': [{'attributes': attrs, 'indices': faces, 'mode': 4, 'material': 0}]},
                      {'primitives': [{'attributes': {'POSITION': p}, 'indices': lines, 'mode': 1}]}]
    return tree, binary


def write_values(tree, binary, accessor, code, values):
    info = tree['accessors'][accessor]; view = tree['bufferViews'][info['bufferView']]
    raw = struct.pack('<' + code * len(values), *values)
    start = view.get('byteOffset', 0)+info.get('byteOffset', 0)
    binary[start:start+len(raw)] = raw


class SurfacePreservationTests(unittest.TestCase):
    def test_all_scenes_and_strided_non_normal_attributes_are_checked(self):
        tree, binary = fixture(); data = encode(tree, binary)
        checked = verify_surfaces(data, data)
        self.assertEqual(checked['scenes'], 2)
        self.assertEqual(checked['all_scene_triangles'], 4)
        self.assertEqual(checked['all_scene_line_segments'], 2)
        self.assertEqual(checked['all_scene_position_corners'], 16)

    def test_normal_material_and_primitive_layout_changes_are_allowed(self):
        tree, binary = fixture(); before = encode(tree, binary)
        tree['materials'][0]['pbrMetallicRoughness']['baseColorFactor'] = [.9, .8, .7, 1]
        n = tree['meshes'][0]['primitives'][0]['attributes']['NORMAL']
        write_values(tree, binary, n, 'f', [.3, .4, .5]*4)
        primitive = tree['meshes'][0]['primitives'][0]
        original = primitive['indices']
        first = copy.deepcopy(tree['accessors'][original]); first['count'] = 3
        second = copy.deepcopy(first); second['byteOffset'] = 6
        primitive['indices'] = len(tree['accessors']); tree['accessors'].append(first)
        tree['accessors'].append(second)
        tree['meshes'][0]['primitives'].append({**primitive, 'indices': len(tree['accessors'])-1})
        self.assertTrue(verify_surfaces(before, encode(tree, binary))['all_scene_oriented_position_and_non_normal_corner_bytes_exact'])

    def test_winding_flip_and_duplicate_loss_are_rejected(self):
        tree, binary = fixture(); before = encode(tree, binary)
        index = tree['meshes'][0]['primitives'][0]['indices']
        write_values(tree, binary, index, 'H', [0, 2, 1, 0, 2, 3])
        with self.assertRaisesRegex(AssertionError, 'surface multiset'):
            verify_surfaces(before, encode(tree, binary))
        tree, binary = fixture(); before = encode(tree, binary)
        tree['accessors'][index]['count'] = 3
        with self.assertRaisesRegex(AssertionError, 'surface multiset'):
            verify_surfaces(before, encode(tree, binary))

    def test_auxiliary_scene_line_loss_is_rejected(self):
        tree, binary = fixture(); before = encode(tree, binary)
        tree['scenes'][1]['nodes'] = [0]
        with self.assertRaisesRegex(AssertionError, 'surface multiset.*scene'):
            verify_surfaces(before, encode(tree, binary))

    def test_effective_nested_trs_and_matrix_are_compared(self):
        tree, binary = fixture(); before = encode(tree, binary)
        tree['nodes'][0] = {'matrix': [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 2, 3, 4, 1], 'children': [1]}
        tree['nodes'][1] = {'mesh': 0, 'matrix': [-1, 0, 0, 0, 0, -1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}
        self.assertEqual(verify_surfaces(before, encode(tree, binary))['max_effective_transform_error'], 0)
        tree['nodes'][0]['matrix'][12] += 1e-6
        with self.assertRaisesRegex(AssertionError, 'instance transform'):
            verify_surfaces(before, encode(tree, binary))

    def test_uv_normalization_and_signed_zero_corner_changes_are_rejected(self):
        original_tree, original_binary = fixture(); before = encode(original_tree, original_binary)
        attrs = original_tree['meshes'][0]['primitives'][0]['attributes']
        for modification in ('uv', 'normalized', 'signed_zero'):
            tree, binary = copy.deepcopy(original_tree), bytearray(original_binary)
            if modification == 'uv':
                write_values(tree, binary, attrs['TEXCOORD_0'], 'f', [.01])
            elif modification == 'normalized':
                tree['accessors'][attrs['COLOR_0']]['normalized'] = False
            else:
                write_values(tree, binary, attrs['POSITION'], 'f', [0., 0., 0.])
            with self.subTest(modification=modification), self.assertRaisesRegex(AssertionError, 'surface multiset'):
                verify_surfaces(before, encode(tree, binary))

    def test_corner_motion_with_unchanged_bounds_is_rejected(self):
        tree, binary = fixture(); before = encode(tree, binary)
        position = tree['meshes'][0]['primitives'][0]['attributes']['POSITION']
        # Other corners still establish x/y bounds [0, 1].
        write_values(tree, binary, position, 'f', [.01, .01, 0.])
        with self.assertRaisesRegex(AssertionError, 'surface multiset'):
            verify_surfaces(before, encode(tree, binary))

    def test_repeated_instance_multiplicity_is_preserved(self):
        tree, binary = fixture()
        tree['nodes'].append({'mesh': 0, 'translation': [2, 3, 4], 'rotation': [0, 0, 1, 0]})
        tree['scenes'][0]['nodes'].append(3); before = encode(tree, binary)
        self.assertEqual(verify_surfaces(before, before)['scene_details'][0]['source']['triangles'], 4)
        tree['scenes'][0]['nodes'].remove(3)
        with self.assertRaisesRegex(AssertionError, 'surface multiset'):
            verify_surfaces(before, encode(tree, binary))

    def test_triangle_cyclic_origins_preserve_attribute_association(self):
        tree, binary = fixture(); before = encode(tree, binary)
        index = tree['meshes'][0]['primitives'][0]['indices']
        write_values(tree, binary, index, 'H', [1, 2, 0, 2, 3, 0])
        verify_surfaces(before, encode(tree, binary))


if __name__ == '__main__':
    unittest.main()
