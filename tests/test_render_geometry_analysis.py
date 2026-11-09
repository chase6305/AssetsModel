import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
AVAILABLE = all(importlib.util.find_spec(x) for x in ('numpy', 'trimesh', 'collada', 'scipy'))
if AVAILABLE:
    import numpy as np
    from convert_glb import GLBWriter
    from analyze_render_geometry import audit, edge_findings


@unittest.skipUnless(AVAILABLE, 'Optional geometry dependencies not installed')
class RenderGeometryAnalysisTests(unittest.TestCase):
    def test_reports_opposing_normals_without_rewriting_mesh(self):
        writer = GLBWriter('inverted-normals')
        writer.add('surface', [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]], [[0, 0, -1]] * 3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'surface.glb'
            data = writer.export()
            path.write_bytes(data)
            result = audit(path)
            self.assertEqual(result['faces_mean_corner_normal_opposes_winding'], 1)
            self.assertEqual(path.read_bytes(), data)

    def test_separates_intentional_reverse_surface_from_same_winding_duplicate(self):
        writer = GLBWriter('double-sided-cad')
        writer.add('surface', [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2], [0, 2, 1]],
                   [[0, 0, 1], [0, 0, 1], [0, 0, 1]])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'surface.glb'
            path.write_bytes(writer.export())
            result = audit(path)
            self.assertEqual(result['duplicate_faces_opposite_winding'], 1)
            self.assertEqual(result['duplicate_faces_same_winding'], 0)

    def test_coplanar_normal_jump_is_reported_but_geometric_hard_edge_is_not(self):
        faces = np.asarray([[0, 1, 2], [2, 1, 3]])
        normals = np.asarray([[[0, 0, 1]] * 3, [[1, 0, 0]] * 3], dtype=float)
        coplanar = np.asarray([[0, 0, 1], [0, 0, 1]], dtype=float)
        sharp = np.asarray([[0, 0, 1], [1, 0, 0]], dtype=float)
        self.assertEqual(edge_findings(faces, coplanar, normals)['normal_seam_edges_on_near_coplanar_faces'], 1)
        self.assertEqual(edge_findings(faces, sharp, normals)['normal_seam_edges_on_near_coplanar_faces'], 0)


if __name__ == '__main__':
    unittest.main()
