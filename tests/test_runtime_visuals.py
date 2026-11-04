"""Consumer checks must notice lost PBR, hard edges, and scene instances."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
AVAILABLE = all(importlib.util.find_spec(name) for name in ('numpy', 'trimesh', 'yourdfpy', 'collada', 'scipy'))
if AVAILABLE:
    import numpy as np
    import trimesh
    import yourdfpy
    from convert_glb import GLBWriter
    from verify_runtime_visuals import verify_glb_scene, verify_urdf_scene


@unittest.skipUnless(AVAILABLE, 'Optional mesh consumer dependencies not installed')
class RuntimeVisualTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'visual.glb'

    def fixture(self, instances=False):
        # Two triangles share the same edge positions but have independent,
        # deliberately tilted corner normals. Geometric normal recomputation
        # or positional welding therefore destroys a visible authored seam.
        positions = [[0, 0, 0], [1, 0, 0], [0, 1, 0],
                     [1, 0, 0], [0, 0, 0], [0, -1, .5]]
        normals = np.array([[.2, 0, 1]] * 3 + [[-.2, .4, 1]] * 3)
        normals /= np.linalg.norm(normals, axis=1, keepdims=True)
        material = {'name': 'embedded blue', 'pbrMetallicRoughness': {
            'baseColorFactor': [.212345, .05, .6, 1],
            'metallicFactor': .37, 'roughnessFactor': .23}, 'doubleSided': True}
        writer = GLBWriter('runtime consumer negative controls')
        writer.add('authored surface', positions, [[0, 1, 2], [3, 4, 5]], normals, material)
        if instances:
            writer.tree['nodes'].append({'name': 'copy', 'mesh': 0, 'translation': [2, 0, 0]})
            writer.tree['scenes'][0]['nodes'].append(1)
        self.path.write_bytes(writer.export())
        return trimesh.load_scene(self.path)

    def test_default_loader_retains_authored_normals_and_linear_pbr(self):
        self.fixture()
        checked = verify_glb_scene(self.path)
        self.assertEqual(checked['triangles'], 2)
        self.assertEqual(checked['max_normal_component_error'], 0)
        self.assertLessEqual(checked['max_linear_basecolor_rounding_error'], .5 / 255)

    def test_reindexing_and_cyclic_triangle_rotation_are_accepted(self):
        scene = self.fixture()
        name = next(iter(scene.geometry))
        old = scene.geometry[name]
        order = np.array([5, 3, 1, 0, 4, 2])
        inverse = np.argsort(order)
        replacement = trimesh.Trimesh(vertices=old.vertices[order],
            faces=inverse[old.faces[::-1, [1, 2, 0]]],
            vertex_normals=old.vertex_normals[order], visual=old.visual.copy(), process=False)
        scene.geometry[name] = replacement
        self.assertEqual(verify_glb_scene(self.path, scene)['max_normal_component_error'], 0)

    def test_recomputed_normals_and_missing_authored_array_are_rejected(self):
        scene = self.fixture()
        mesh = next(iter(scene.geometry.values()))
        mesh.vertex_normals = np.tile([0., 0., 1.], (len(mesh.vertices), 1))
        with self.assertRaisesRegex(AssertionError, 'normal'):
            verify_glb_scene(self.path, scene)
        scene = trimesh.load_scene(self.path)
        next(iter(scene.geometry.values()))._cache.delete('vertex_normals')
        with self.assertRaisesRegex(AssertionError, 'NORMAL array'):
            verify_glb_scene(self.path, scene)

    def test_lost_pbr_or_changed_material_is_rejected(self):
        scene = self.fixture()
        next(iter(scene.geometry.values())).visual = trimesh.visual.ColorVisuals(face_colors=[255, 0, 0, 255])
        with self.assertRaisesRegex(AssertionError, 'PBR TextureVisuals'):
            verify_glb_scene(self.path, scene)
        scene = trimesh.load_scene(self.path)
        next(iter(scene.geometry.values())).visual.material.roughnessFactor = .6
        with self.assertRaisesRegex(AssertionError, 'PBR/material'):
            verify_glb_scene(self.path, scene)

    def test_optional_material_extensions_are_not_silently_ignored(self):
        self.fixture()
        from verify_normal_updates import independent_read_glb
        from refine_materials import encode_glb
        tree, binary = independent_read_glb(self.path.read_bytes())
        tree['materials'][0]['extensions'] = {'KHR_materials_clearcoat': {'clearcoatFactor': 1}}
        tree['extensionsUsed'] = ['KHR_materials_clearcoat']
        self.path.write_bytes(encode_glb(tree, binary))
        with self.assertRaisesRegex(ValueError, 'profile|extensions'):
            verify_glb_scene(self.path)

    def test_instanced_geometry_multiplicity_and_transform_are_checked(self):
        scene = self.fixture(instances=True)
        self.assertEqual(verify_glb_scene(self.path, scene)['triangles'], 4)
        removed = trimesh.load_scene(self.path)
        removed.graph.transforms.remove_node('copy')
        with self.assertRaisesRegex(AssertionError, 'partition|multiplicity'):
            verify_glb_scene(self.path, removed)
        frame, geometry = scene.graph.get('copy')
        frame = frame.copy()
        frame[0, 3] += .1
        scene.graph.update(frame_to='copy', matrix=frame, geometry=geometry)
        with self.assertRaisesRegex(AssertionError, 'transform'):
            verify_glb_scene(self.path, scene)

    def test_coincident_surfaces_keep_each_authored_normal_field(self):
        writer = GLBWriter('coincident authored surfaces')
        positions = [[0, 0, 0], [1, 0, 0], [0, 1, 0]] * 2
        normals = np.array([[0., 0., 1.]] * 3 + [[0., .2, 1.]] * 3)
        normals /= np.linalg.norm(normals, axis=1, keepdims=True)
        writer.add('coincident sheets', positions, [[0, 1, 2], [3, 4, 5]], normals,
                   {'pbrMetallicRoughness': {'baseColorFactor': [.4, .5, .6, 1]}})
        self.path.write_bytes(writer.export())
        scene = trimesh.load_scene(self.path)
        self.assertEqual(verify_glb_scene(self.path, scene)['duplicate_position_triangle_records'], 1)
        mesh = next(iter(scene.geometry.values()))
        mesh.vertex_normals = np.tile([0., 0., 1.], (6, 1))
        with self.assertRaisesRegex(AssertionError, 'Duplicate-position authored normal multiplicity'):
            verify_glb_scene(self.path, scene)

    def test_degenerate_corner_normals_allow_cyclic_face_rotation(self):
        writer = GLBWriter('repeated-position triangle')
        writer.add('degenerate CAD face', [[0, 0, 0], [0, 0, 0], [1, 0, 0]],
                   [[0, 1, 2]], [[0, 0, 1], [0, 1, 0], [1, 0, 0]],
                   {'pbrMetallicRoughness': {'baseColorFactor': [.2, .3, .4, 1]}})
        self.path.write_bytes(writer.export())
        scene = trimesh.load_scene(self.path)
        mesh = next(iter(scene.geometry.values()))
        # Replacing the face array invalidates trimesh's normal cache; save the
        # authored array and restore it, just as a retaining importer would.
        normals = mesh.vertex_normals.copy()
        mesh.faces = mesh.faces[:, [1, 2, 0]]
        mesh.vertex_normals = normals
        self.assertEqual(verify_glb_scene(self.path, scene)['max_normal_component_error'], 0)

    def test_default_yourdfpy_keeps_embedded_pbr_despite_urdf_color(self):
        self.fixture(instances=True)
        path = self.path.with_suffix('.urdf')
        path.write_text('''<robot name="runtime consumer">
          <link name="base">
            <visual name="first"><origin xyz=".3 -.2 .4" rpy=".2 -.3 .4"/>
              <geometry><mesh filename="visual.glb"/></geometry>
              <material name="override"><color rgba="1 0 0 1"/></material>
            </visual>
            <visual name="second"><origin xyz="0 0 2"/>
              <geometry><mesh filename="visual.glb"/></geometry>
            </visual>
          </link></robot>''')
        checked = verify_urdf_scene(path)
        self.assertEqual(checked['triangles'], 8)
        self.assertTrue(checked['embedded_pbr_texture_visuals'])
        self.assertLess(checked['max_transform_error'], 1e-12)
        model = yourdfpy.URDF.load(str(path))
        next(iter(model.scene.geometry.values())).vertex_normals = np.tile([0., 0., 1.], (6, 1))
        with self.assertRaisesRegex(AssertionError, 'normal'):
            verify_urdf_scene(path, model)


if __name__ == '__main__':
    unittest.main()
