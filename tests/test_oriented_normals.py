"""CAD double sheets need smooth local fans without front/back cancellation."""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
AVAILABLE = importlib.util.find_spec('numpy') is not None
if AVAILABLE:
    import numpy as np
    from smooth_oriented_sheets import smooth_oriented_normals


@unittest.skipUnless(AVAILABLE, 'Optional numpy dependency not installed')
class OrientedNormalsTests(unittest.TestCase):
    def face_normals(self, vertices, faces):
        p = np.asarray(vertices)[np.asarray(faces)]
        cross = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
        return cross / np.linalg.norm(cross, axis=1)[:, None]

    def cylinder(self, sections=24):
        # Independent triangle vertices reproduce STL/CAD exact-coordinate seams.
        theta = np.arange(sections) * 2 * np.pi / sections
        bottom = np.column_stack((np.cos(theta), np.sin(theta), np.zeros(sections)))
        top = bottom.copy(); top[:, 2] = 2
        vertices = np.vstack((bottom, top))
        faces = []
        for i in range(sections):
            j = (i + 1) % sections
            faces.extend(([i, j, sections + j], [i, sections + j, sections + i]))
        faces = np.asarray(faces, dtype=np.int64)
        positions = vertices[faces].reshape(-1, 3)
        return positions, np.arange(len(positions)).reshape(-1, 3)

    def test_coincident_front_back_cylinder_sheets_smooth_separately(self):
        vertices, front = self.cylinder()
        back = front[:, [0, 2, 1]]
        faces = np.vstack((front, back))
        saved_vertices, saved_faces = vertices.copy(), faces.copy()
        normals = smooth_oriented_normals(vertices, faces).reshape(-1, 3, 3)
        # Reversed triangles receive the same smooth normal with opposite sign.
        np.testing.assert_allclose(normals[len(front):], -normals[:len(front), [0, 2, 1]], atol=1e-14)
        self.assertGreater(int((np.ptp(normals[:len(front)], axis=1).max(axis=1) > 1e-3).sum()), len(front) // 2)
        radial = vertices[front].copy(); radial[:, :, 2] = 0
        radial /= np.linalg.norm(radial, axis=2)[:, :, None]
        self.assertGreater(float(np.einsum('fvc,fvc->fv', normals[:len(front)], radial).min()), .995)
        np.testing.assert_allclose(np.linalg.norm(normals, axis=2), 1, atol=1e-14)
        np.testing.assert_array_equal(vertices, saved_vertices)
        np.testing.assert_array_equal(faces, saved_faces)

    def test_double_sided_cylinder_caps_keep_sharp_rims(self):
        vertices, front = self.cylinder()
        count = 24
        theta = np.arange(count) * 2 * np.pi / count
        ring = np.column_stack((np.cos(theta), np.sin(theta), np.zeros(count)))
        caps = []
        for i in range(count):
            j = (i + 1) % count
            caps.extend(([np.zeros(3), ring[j], ring[i]],
                         [np.asarray([0., 0, 2]), ring[i] + [0, 0, 2], ring[j] + [0, 0, 2]]))
        cap_positions = np.asarray(caps).reshape(-1, 3)
        cap_faces = np.arange(len(cap_positions)).reshape(-1, 3) + len(vertices)
        vertices = np.vstack((vertices, cap_positions))
        front = np.vstack((front, cap_faces))
        faces = np.vstack((front, front[:, [0, 2, 1]]))
        normals = smooth_oriented_normals(vertices, faces).reshape(-1, 3, 3)
        cap_count = len(cap_faces)
        expected_caps = np.repeat(self.face_normals(vertices, cap_faces), 3, axis=0).reshape(-1, 3, 3)
        np.testing.assert_allclose(normals[len(front)-cap_count:len(front)], expected_caps, atol=1e-14)
        np.testing.assert_allclose(normals[len(front):], -normals[:len(front), [0, 2, 1]], atol=1e-14)

    def test_cube_keeps_ninety_degree_hard_edges(self):
        vertices = np.asarray([[x, y, z] for x in (-1., 1.) for y in (-1., 1.) for z in (-1., 1.)])
        faces = np.asarray([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5],
                            [0, 4, 5], [0, 5, 1], [2, 3, 7], [2, 7, 6],
                            [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]])
        expected = np.repeat(self.face_normals(vertices, faces), 3, axis=0)
        np.testing.assert_array_equal(smooth_oriented_normals(vertices, faces), expected)

    def test_point_touching_disconnected_fans_do_not_mix(self):
        # Two nearly parallel triangles share a position but no complete edge.
        vertices = np.asarray([[0., 0, 0], [1, 0, 0], [0, 1, 0],
                               [0, 0, 0], [-1, 0, .1], [0, -1, 0]])
        faces = np.asarray([[0, 1, 2], [3, 4, 5]])
        expected = np.repeat(self.face_normals(vertices, faces), 3, axis=0)
        np.testing.assert_allclose(smooth_oriented_normals(vertices, faces), expected, atol=1e-14)
        self.assertLess(float(np.dot(expected[0], expected[3])), 1)

    def test_ambiguous_nonmanifold_edge_remains_flat(self):
        # Each reverse half-edge chooses the first face, but it has two valid
        # choices. No mutual unique pair exists, so no fan crosses the edge.
        vertices = np.asarray([[0., 0, 0], [1, 0, 0], [0, 1, 0], [0, -1, .1], [0, -1, .2]])
        faces = np.asarray([[0, 1, 2], [1, 0, 3], [1, 0, 4]])
        expected = np.repeat(self.face_normals(vertices, faces), 3, axis=0)
        np.testing.assert_allclose(smooth_oriented_normals(vertices, faces), expected, atol=1e-14)

    def test_same_direction_shared_edge_does_not_connect(self):
        vertices = np.asarray([[0., 0, 0], [1, 0, 0], [0, 1, 0], [0, 1, .1]])
        faces = np.asarray([[0, 1, 2], [0, 1, 3]])
        expected = np.repeat(self.face_normals(vertices, faces), 3, axis=0)
        np.testing.assert_allclose(smooth_oriented_normals(vertices, faces), expected, atol=1e-14)

    def test_fan_normals_are_area_weighted(self):
        vertices = np.asarray([[0., 0, 0], [1, 0, 0], [0, 2, 0], [0, -1, .2]])
        faces = np.asarray([[0, 1, 2], [1, 0, 3]])
        p = vertices[faces]
        cross = np.cross(p[:, 1]-p[:, 0], p[:, 2]-p[:, 0])
        expected = cross.sum(axis=0); expected /= np.linalg.norm(expected)
        normals = smooth_oriented_normals(vertices, faces).reshape(-1, 3, 3)
        for fi, ci in [(0, 0), (0, 1), (1, 0), (1, 1)]:
            np.testing.assert_allclose(normals[fi, ci], expected, atol=1e-14)

    def test_nearby_but_not_exact_edges_are_not_welded(self):
        vertices = np.asarray([[0., 0, 0], [1, 0, 0], [0, 1, 0],
                               [0, 0, 1e-12], [1, 0, 1e-12], [0, -1, .1]])
        faces = np.asarray([[0, 1, 2], [4, 3, 5]])
        expected = np.repeat(self.face_normals(vertices, faces), 3, axis=0)
        np.testing.assert_allclose(smooth_oriented_normals(vertices, faces), expected, atol=1e-14)

    def test_nonmanifold_edge_can_have_two_independent_mutual_pairs(self):
        angles = np.radians([0., 15., 60., 75.])
        vertices = [[0., 0, 0], [1, 0, 0]]
        for i, angle in enumerate(angles):
            sign = 1 if i % 2 == 0 else -1
            vertices.append([0, sign * np.cos(angle), -sign * np.sin(angle)])
        vertices = np.asarray(vertices)
        faces = np.asarray([[0, 1, 2], [1, 0, 3], [0, 1, 4], [1, 0, 5]])
        normals = smooth_oriented_normals(vertices, faces).reshape(-1, 3, 3)
        geometric = self.face_normals(vertices, faces)
        for a, b in ((0, 1), (2, 3)):
            expected = geometric[a] + geometric[b]
            expected /= np.linalg.norm(expected)
            for face in (a, b):
                np.testing.assert_allclose(normals[face, :2], np.tile(expected, (2, 1)), atol=1e-14)
        self.assertLess(float(np.dot(normals[0, 0], normals[2, 0])), .6)

    def test_invalid_angle_indices_and_degenerate_face_are_rejected(self):
        vertices = np.asarray([[0., 0, 0], [1, 0, 0], [0, 1, 0]])
        for angle in (-1, 90, float('nan')):
            with self.assertRaises(ValueError):
                smooth_oriented_normals(vertices, np.asarray([[0, 1, 2]]), angle)
        with self.assertRaises(ValueError):
            smooth_oriented_normals(vertices, np.asarray([[0, 1, 3]]))
        with self.assertRaises(ValueError):
            smooth_oriented_normals(vertices, np.asarray([[0, 1, 1]]))


if __name__ == '__main__':
    unittest.main()
