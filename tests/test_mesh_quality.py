"""Geometry acceptance regressions; skipped when optional mesh tools are absent."""

import importlib.util
import io
import tempfile
import struct
import unittest
from pathlib import Path

AVAILABLE = all(importlib.util.find_spec(name) for name in ("numpy", "trimesh", "collada", "rtree"))
if AVAILABLE:
    import numpy as np
    import trimesh
    spec = importlib.util.spec_from_file_location("optimizer", Path(__file__).resolve().parents[1] / "scripts/optimize_meshes.py")
    optimizer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(optimizer)
    cleanup_spec = importlib.util.spec_from_file_location("cleanup", Path(__file__).resolve().parents[1] / "scripts/cleanup_degenerate.py")
    cleanup = importlib.util.module_from_spec(cleanup_spec)
    cleanup_spec.loader.exec_module(cleanup)


@unittest.skipUnless(AVAILABLE, "Optional mesh dependencies are not installed")
class MeshQualityTests(unittest.TestCase):
    def test_stl_cleanup_keeps_valid_records_byte_for_byte(self):
        good = struct.pack('<12fH', 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 42)
        bad = struct.pack('<12fH', *([0] * 12), 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'part.stl'
            path.write_bytes(b'A' * 80 + struct.pack('<I', 2) + good + bad)
            output, removed = cleanup.clean(path)
        self.assertEqual(removed, 1)
        self.assertEqual(output, b'A' * 80 + struct.pack('<I', 1) + good)

    def test_polygon_cleanup_writes_triangles_with_valid_indices(self):
        xml = f'''<COLLADA xmlns="{optimizer.NS}" version="1.4.1">
          <library_geometries><geometry id="part"><mesh>
          <source id="positions"><float_array id="array" count="12">0 0 0 1 0 0 0 1 0 2 0 0</float_array>
          <technique_common><accessor source="#array" count="4" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
          <vertices id="vertices"><input semantic="POSITION" source="#positions"/></vertices>
          <polylist count="2" material="metal"><input semantic="VERTEX" source="#vertices" offset="0"/><vcount>3 3</vcount><p>0 1 2 0 1 3</p></polylist>
          </mesh></geometry></library_geometries></COLLADA>'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'part.dae'
            path.write_text(xml)
            output, removed = cleanup.clean(path)
        self.assertEqual(removed, 1)
        doc = optimizer.collada.Collada(io.BytesIO(output))
        primitive = doc.geometries[0].primitives[0]
        self.assertIsInstance(primitive, optimizer.collada.triangleset.TriangleSet)
        self.assertEqual(primitive.material, 'metal')
        np.testing.assert_array_equal(primitive.vertex[primitive.vertex_index], [[[0, 0, 0], [1, 0, 0], [0, 1, 0]]])

    def test_multimaterial_dae_and_auxiliary_lines_round_trip(self):
        xml = optimizer.ET.fromstring(f'<COLLADA xmlns="{optimizer.NS}" version="1.4.1"><library_geometries><geometry id="part"><mesh/></geometry></library_geometries></COLLADA>')
        geometry = xml.find(f".//{optimizer.tag('geometry')}")
        first, second = trimesh.creation.box(), trimesh.creation.box()
        second.apply_translation([3, 0, 0])
        line_positions = np.array([[0., 0., 0.], [3., 2., 1.]])
        optimizer.replace_dae_geometry(geometry, [
            (first, np.repeat(first.face_normals[:, None, :], 3, axis=1), 'red'),
            (second, np.repeat(second.face_normals[:, None, :], 3, axis=1), 'blue'),
        ], [(line_positions, np.array([[0, 1]]), 'edge')])
        mesh_xml = geometry.find(optimizer.tag('mesh'))
        self.assertEqual(len(mesh_xml.findall(optimizer.tag('vertices'))), 1)
        loaded = optimizer.collada.Collada(io.BytesIO(optimizer.ET.tostring(xml)))
        primitives = loaded.geometries[0].primitives
        self.assertEqual([p.material for p in primitives], ['red', 'blue', 'edge'])
        np.testing.assert_allclose(primitives[0].vertex[primitives[0].vertex_index], first.triangles)
        np.testing.assert_allclose(primitives[1].vertex[primitives[1].vertex_index], second.triangles)
        np.testing.assert_allclose(primitives[2].vertex[primitives[2].vertex_index][0], line_positions)

    def test_small_triangle_surface_has_zero_self_distance(self):
        mesh = trimesh.Trimesh([[0, 0, 0], [0.0001, 0, 0], [0, 0.0001, 0]], [[0, 1, 2]], process=False)
        points = np.array([[0.000025, 0.000025, 0], [0.00001, 0.00002, 0]])
        np.testing.assert_allclose(optimizer.surface_distances(mesh, points), 0, atol=1e-14)

    def test_translation_exceeding_tolerance_is_rejected(self):
        original = trimesh.creation.box()
        shifted = original.copy()
        shifted.apply_translation([0.01, 0, 0])
        accepted, _ = optimizer.assess(original, shifted, tolerance=0.0005, samples=50)
        self.assertFalse(accepted)

    def test_losing_a_separate_part_is_rejected(self):
        first, second = trimesh.creation.box(), trimesh.creation.box()
        second.apply_translation([3, 0, 0])
        original = trimesh.util.concatenate([first, second])
        accepted, metrics = optimizer.assess(original, first, tolerance=0.0005, samples=50)
        self.assertFalse(accepted)
        self.assertEqual(metrics["reason"], "component count changed")

    def test_duplicate_and_zero_area_faces_are_cleaned(self):
        mesh = optimizer.clean_mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 0]],
                                    [[0, 1, 2], [3, 1, 2], [0, 0, 1]])
        self.assertEqual(len(mesh.faces), 1)
        self.assertAlmostEqual(mesh.area, 0.5)


if __name__ == "__main__":
    unittest.main()
