"""Runtime exports must retain upstream redistribution and provenance notices."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from export_assets import export_assets


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


if __name__ == '__main__':
    unittest.main()
