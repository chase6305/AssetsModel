"""Operational guards for the source-hash-locked M20 normal refinement."""
from contextlib import redirect_stderr
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import refine_m20_planar_normals as refinement


class M20OperationalTests(unittest.TestCase):
    def arguments(self, root, report):
        return ["refine_m20_planar_normals.py", "--root", str(root), "--apply",
                "--backup-dir", str(root.parent / "backup"), "--report", str(report)]

    def test_invalid_report_parent_prevents_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked = root / "parent-is-a-file"
            blocked.write_bytes(b"existing unrelated file")
            with patch.object(sys, "argv", self.arguments(root, blocked / "report.json")), \
                    patch.object(refinement, "run") as run, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exit:
                    refinement.main()
                self.assertEqual(exit.exception.code, 2)
                run.assert_not_called()
            self.assertEqual(blocked.read_bytes(), b"existing unrelated file")

    def test_unwritable_report_prevents_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            report.write_bytes(b"previous report")
            with patch.object(sys, "argv", self.arguments(root, report)), \
                    patch.object(refinement.os, "open", side_effect=PermissionError("report is read-only")), \
                    patch.object(refinement, "run") as run, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exit:
                    refinement.main()
                self.assertEqual(exit.exception.code, 2)
                run.assert_not_called()
            self.assertEqual(report.read_bytes(), b"previous report")

    def test_preflight_preserves_existing_report_bytes_and_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            report.write_bytes(b"previous report remains intact")
            before = report.stat().st_mtime_ns
            self.assertEqual(refinement.preflight_report(root, report), report)
            self.assertEqual(report.read_bytes(), b"previous report remains intact")
            self.assertEqual(report.stat().st_mtime_ns, before)
            self.assertEqual(list(root.iterdir()), [report])

    def test_exact_target_apply_does_not_repair_rewrite_or_create_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, backup = base / "assets", base / "backup"
            target = root / refinement.MODEL / refinement.VISUAL
            target.parent.mkdir(parents=True)
            payload = b"synthetic already accepted target"
            target.write_bytes(payload)
            os.utime(target, ns=(1700000000000000000, 1700000000000000000))
            mtime = target.stat().st_mtime_ns
            with patch.object(refinement, "TARGET_SHA", hashlib.sha256(payload).hexdigest()), \
                    patch.object(refinement, "repair") as repair:
                for _ in range(2):
                    report = refinement.run(root, apply=True, backup_dir=backup)
                    self.assertEqual(report["status"], "unchanged")
                    self.assertFalse(report["applied"])
                    self.assertTrue(report["exact_reviewed_target"])
                repair.assert_not_called()
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(target.stat().st_mtime_ns, mtime)
            self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()
