"""Runtime exports must retain upstream redistribution and provenance notices."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from export_assets import export_assets
from decompose_robot_collisions import rewrite_urdf


class ExportNoticesTests(unittest.TestCase):
    def test_referenced_only_export_keeps_upstream_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            model = root / 'RobotModel' / 'Vendor' / 'Model'
            model.mkdir(parents=True)
            (model / 'Model.urdf').write_text('<robot name="sample"><link name="base"/></robot>')
            required = {'LICENSE': 'upstream copyright\n', 'NOTICE': 'upstream source\n',
                        'provenance.json': json.dumps({'source_commit': 'fixed-revision'})}
            for name, content in required.items():
                (model / name).write_text(content)
            (model / 'unreferenced.bin').write_bytes(b'not a runtime dependency')
            output = Path(directory) / 'export'
            report = export_assets(root, ['RobotModel/Vendor/Model'], output, referenced_only=True)
            exported = output / 'RobotModel' / 'Vendor' / 'Model'
            for name, content in required.items():
                self.assertEqual((exported / name).read_text(), content)
            self.assertFalse((exported / 'unreferenced.bin').exists())
            self.assertEqual(report['summary']['metadata_files'], 3)
            self.assertEqual(len(report['files']), 4)

    def test_convex_parts_keep_collision_origin_and_are_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            model = root / 'RobotModel' / 'Vendor' / 'Model'
            legacy = model / 'Collision/glb/part.glb'
            legacy.parent.mkdir(parents=True)
            legacy.touch()
            urdf = model / 'Model.urdf'
            original = b'''<robot name="sample">
  <link name="base">
    <!-- Primitive collider stays on the same link. -->
    <collision><geometry><box size="1 1 1"/></geometry></collision>
    <collision name="part">
      <origin xyz="1 2 3" rpy="0 0 1"/>
      <geometry><mesh filename="Collision/glb/part.glb" scale="1 2 3"/></geometry>
    </collision>
  </link>
</robot>
'''
            hull_dir = model / 'Collision/obj'
            hull_dir.mkdir()
            parts = [hull_dir / name for name in ('part.obj', 'part_hull_2.obj')]
            for part in parts:
                part.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
            updated = rewrite_urdf(urdf, original, {legacy: parts})
            self.assertEqual(updated.count(b'<origin xyz="1 2 3" rpy="0 0 1"/>'), 2)
            self.assertEqual(updated.count(b'scale="1 2 3"'), 2)
            self.assertIn(b'<collision name="part_hull_2">', updated)
            self.assertIn(b'<collision><geometry><box size="1 1 1"/></geometry></collision>', updated)
            urdf.write_bytes(updated)
            output = Path(directory) / 'export'
            report = export_assets(root, [model.relative_to(root)], output, referenced_only=True)
            self.assertEqual(report['summary']['objs'], 2)
            self.assertEqual(report['summary']['metadata_files'], 0)
            for part in parts:
                self.assertEqual((output / part.relative_to(root)).read_bytes(), part.read_bytes())


if __name__ == '__main__':
    unittest.main()
