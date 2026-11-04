"""Negative controls for corner masks and visible normal discontinuities."""
import copy
import hashlib
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from verify_masked_normals import corner_rows, inspect_edges, verify_masked_update
from verify_surface_preservation import read_container
from test_surface_preservation import fixture, encode, write_values


class MaskedNormalTests(unittest.TestCase):
    def edge_pair(self, geometric_b, output_a, output_b, old_b=(0., 0., 1.)):
        a = {'primitive': [0, 0], 'corners': [0, 1], 'start_position': (0, 0, 0),
             'end_position': (1, 0, 0), 'face_normal': (0., 0., 1.),
             'old_normals': [(0., 0., 1.)]*2, 'new_normals': [output_a]*2, 'selected': [True]*2}
        b = {'primitive': [0, 0], 'corners': [3, 4], 'start_position': (1, 0, 0),
             'end_position': (0, 0, 0), 'face_normal': geometric_b,
             'old_normals': [old_b]*2, 'new_normals': [output_b]*2, 'selected': [False]*2}
        return {((0, 0, 0), (1, 0, 0)): [a, b]}

    def test_small_visible_seam_is_found_below_old_15_degree_gate(self):
        tilted = (0., math.sin(math.radians(5)), math.cos(math.radians(5)))
        data = inspect_edges(self.edge_pair((0., 0., 1.), tilted, (0., 0., 1.)))
        self.assertEqual(data['near_coplanar_10_to_15_seams_added'], 0)
        self.assertEqual(data['near_coplanar_gap_growth_gt_1deg'], 2)
        self.assertEqual(data['near_coplanar_new_gap_above_1deg'], 2)
        self.assertAlmostEqual(data['near_coplanar_max_gap_growth_deg'], 5)

    def test_authored_normal_at_real_geometric_hard_edge_cannot_be_merged(self):
        data = inspect_edges(self.edge_pair((0., 1., 0.), (0., 0., 1.),
                                            (0., 0., 1.), old_b=(0., 1., 0.)))
        self.assertEqual(data['geometric_and_authored_30deg_hard_endpoint_pairs'], 2)
        self.assertEqual(data['hard_endpoint_pairs_below_30deg_after'], 2)

    def test_shared_vertex_update_cannot_escape_single_corner_mask(self):
        tree, binary = fixture(); source = encode(tree, binary)
        primitive = tree['meshes'][0]['primitives'][0]
        normal_index = primitive['attributes']['NORMAL']
        write_values(tree, binary, normal_index, 'f', [0., .1, math.sqrt(.99)])
        output = encode(tree, binary)
        before, before_bin = read_container(source); after, after_bin = read_container(output)
        old_rows, _ = corner_rows(before, before_bin, before['meshes'][0]['primitives'][0], 'NORMAL')
        new_rows, _ = corner_rows(after, after_bin, after['meshes'][0]['primitives'][0], 'NORMAL')
        mask = {'primitives': [{'mesh': 0, 'primitive': 0, 'material_name': None,
                 'corner_count': 6, 'selected_corner_count': 1, 'selected_corner_indices': [0],
                 'source_corner_normals_sha256': hashlib.sha256(b''.join(old_rows)).hexdigest(),
                 'expected_output_corner_normals_sha256': hashlib.sha256(b''.join(new_rows)).hexdigest()}]}
        # Vertex 0 is also corner 3. Hashes alone cannot authorize that change.
        with self.assertRaisesRegex(AssertionError, 'Unselected corner NORMAL'):
            verify_masked_update(source, output, mask)
        full_mask = copy.deepcopy(mask)
        full_mask['primitives'][0].update(selected_corner_count=2, selected_corner_indices=[0, 3])
        self.assertEqual(verify_masked_update(source, output, full_mask)['changed_normal_corners'], 2)


if __name__ == '__main__':
    unittest.main()
