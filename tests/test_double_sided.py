import sys
import unittest
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
AVAILABLE = all(importlib.util.find_spec(name) for name in ('numpy', 'trimesh', 'collada', 'scipy'))
if AVAILABLE:
    import numpy as np
    from compact_double_sided import compact_pairs
    from convert_glb import GLBWriter
    from glb_format import read_glb, validate_glb
    from audit_asset_quality import array


@unittest.skipUnless(AVAILABLE, 'Optional geometry dependencies not installed')
class DoubleSidedTests(unittest.TestCase):
    def fixture(self, almost=False, extra_attribute=False, alpha=False, unpaired=False, materials=False):
        vertices = np.array([[0,0,0],[1,0,0],[0,1,0],[0,0,0],[0,1,0],[1,0,0]],dtype=np.float32)
        normals = np.array([[0,0,1]]*3+[[0,0,-1]]*3,dtype=np.float32)
        if almost:
            normals[3,0] = 1e-7
        material = {'name':'paint','pbrMetallicRoughness':{'baseColorFactor':[.2,.3,.4,1],'metallicFactor':0,'roughnessFactor':1}}
        if alpha:
            material['alphaMode']='BLEND'
        writer = GLBWriter('pair.stl')
        faces = np.array([[0,1,2],[3,4,5]])
        if unpaired:
            vertices=np.vstack([vertices,vertices[:3]+[0,0,1]])
            normals=np.vstack([normals,normals[:3]])
            faces=np.vstack([faces,[6,7,8]])
        if materials:
            writer.add('front',vertices,faces[:1],normals,material)
            writer.add('back',vertices,faces[1:2],normals,{'name':'other'})
        else:
            primitive=writer.add('pair',vertices,faces,normals,material)
            if extra_attribute:
                primitive['attributes']['TEXCOORD_0']=writer.accessor(np.zeros((len(vertices),2),dtype='<f4'),5126,'VEC2',34962)
        return writer.export()

    def test_exact_pair_becomes_one_double_sided_triangle(self):
        payload=self.fixture();output,stats=compact_pairs(payload)
        self.assertEqual(stats['removed_triangles'],1)
        self.assertEqual(validate_glb(output)['faces'],1)
        tree,binary=read_glb(output);primitive=tree['meshes'][0]['primitives'][0]
        self.assertTrue(tree['materials'][primitive['material']]['doubleSided'])
        self.assertEqual(compact_pairs(output)[0],output)

    def test_almost_negative_normals_are_retained(self):
        payload=self.fixture(almost=True)
        self.assertEqual(compact_pairs(payload),(payload,{'removed_triangles':0}))

    def test_uv_transparency_and_different_materials_are_retained(self):
        for setting in ('extra_attribute','alpha','materials'):
            with self.subTest(setting=setting):
                payload=self.fixture(**{setting:True})
                self.assertEqual(compact_pairs(payload),(payload,{'removed_triangles':0}))

    def test_unpaired_faces_retain_single_sided_original_material(self):
        payload=self.fixture(unpaired=True);output,stats=compact_pairs(payload)
        self.assertEqual(stats['removed_triangles'],1)
        tree,binary=read_glb(output);primitives=tree['meshes'][0]['primitives']
        self.assertEqual(len(primitives),2)
        self.assertFalse(tree['materials'][primitives[0]['material']].get('doubleSided',False))
        self.assertTrue(tree['materials'][primitives[1]['material']]['doubleSided'])
        positions=array(tree,binary,primitives[0]['attributes']['POSITION'])
        np.testing.assert_array_equal(positions[:,2],1)

    def test_animated_or_transformed_assets_are_rejected(self):
        from batch_glb import export
        for key, value in [('translation', [1, 0, 0]), ('children', [1])]:
            tree, binary = read_glb(self.fixture())
            tree['nodes'][0][key] = value
            with self.assertRaisesRegex(ValueError, 'identity-node'):
                compact_pairs(export(tree, binary))
        tree, binary = read_glb(self.fixture())
        tree['animations'] = [{'name': 'unsupported'}]
        with self.assertRaisesRegex(ValueError, 'identity-node'):
            compact_pairs(export(tree, binary))

    def test_cli_never_changes_a_collision_or_shared_mesh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'RobotModel' / 'Example'
            folder.mkdir(parents=True)
            target = folder / 'part.glb'
            original = self.fixture()
            target.write_bytes(original)
            visual = '<visual><geometry><mesh filename="part.glb"/></geometry></visual>'
            collision = '<collision><geometry><mesh filename="part.glb"/></geometry></collision>'
            for content in (collision, visual + collision):
                (folder / 'model.urdf').write_text('<robot name="example"><link name="base">'+content+'</link></robot>')
                report = root / 'report.json'
                script = Path(__file__).resolve().parents[1] / 'scripts' / 'compact_double_sided.py'
                result = subprocess.run([sys.executable, str(script), str(target), '--root', str(root),
                                         '--report', str(report)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(report.read_text())['files'][0]['status'], 'skipped')
                self.assertEqual(target.read_bytes(), original)


if __name__=='__main__':unittest.main()
