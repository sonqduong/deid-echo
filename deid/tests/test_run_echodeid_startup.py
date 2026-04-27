import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deidecho_run import run_echodeid


class TestRunEchoDeidStartup(unittest.TestCase):
    def test_assess_jpeg_baseline_backend_python_only_skips_preflight(self):
        status = run_echodeid.assess_jpeg_baseline_backend(
            run_echodeid.JPEG_BASELINE_BACKEND_PYTHON_ONLY
        )

        self.assertEqual(status["status"], "skipped")
        self.assertEqual(status["backend"], run_echodeid.JPEG_BASELINE_BACKEND_PYTHON_ONLY)
        self.assertIn("preflight skipped", status["message"])

    def test_assess_jpeg_baseline_backend_auto_warns_when_unavailable(self):
        diagnostics = {
            "available": False,
            "java_path": "",
            "javac_path": "",
            "jar_path": "",
            "class_dir": "",
            "error": "javac not found on PATH",
        }
        with patch.object(
            run_echodeid, "inspect_pixelmed_runtime", return_value=diagnostics
        ):
            status = run_echodeid.assess_jpeg_baseline_backend(
                run_echodeid.JPEG_BASELINE_BACKEND_AUTO
            )

        self.assertEqual(status["status"], "unavailable")
        self.assertFalse(status["available"])
        self.assertIn("fall back to python_jpeg_baseline", status["message"])
        self.assertIn("javac not found on PATH", status["message"])

    def test_assess_jpeg_baseline_backend_require_pixelmed_errors_when_unavailable(self):
        diagnostics = {
            "available": False,
            "java_path": "",
            "javac_path": "",
            "jar_path": "",
            "class_dir": "",
            "error": "java not found on PATH",
        }
        with patch.object(
            run_echodeid, "inspect_pixelmed_runtime", return_value=diagnostics
        ):
            status = run_echodeid.assess_jpeg_baseline_backend(
                run_echodeid.JPEG_BASELINE_BACKEND_REQUIRE_PIXELMED
            )

        self.assertEqual(status["status"], "unavailable")
        self.assertIn("required", status["message"])
        self.assertIn("java not found on PATH", status["message"])

    def test_discover_dicom_files_returns_lexically_sorted_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "b").mkdir()
            (root / "a").mkdir()
            for rel in ("b/2.dcm", "a/3.dcm", "a/1.dcm"):
                path = root / rel
                path.write_text("x", encoding="utf-8")

            files = run_echodeid.discover_dicom_files(root)

        self.assertEqual(
            [str(p.relative_to(root)) for p in files],
            ["a/1.dcm", "a/3.dcm", "b/2.dcm"],
        )

    def test_prepare_todo_files_filters_done_src_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "input"
            log_dir = Path(tmpdir) / "logs"
            root.mkdir()
            log_dir.mkdir()
            for rel in ("c/3.dcm", "a/1.dcm", "b/2.dcm"):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            done = {str(root / "b" / "2.dcm")}
            with patch.object(
                run_echodeid,
                "load_done_src_paths_from_worker_logs",
                return_value=done,
            ):
                dcm_files, total_files, done_src, todo = run_echodeid.prepare_todo_files(
                    root, log_dir
                )

        self.assertEqual(total_files, 3)
        self.assertEqual(done_src, done)
        self.assertEqual(
            [str(p.relative_to(root)) for p in dcm_files],
            ["a/1.dcm", "b/2.dcm", "c/3.dcm"],
        )
        self.assertEqual(
            [str(Path(p).relative_to(root)) for p in todo],
            ["a/1.dcm", "c/3.dcm"],
        )

    def test_prepare_todo_files_subsample_stays_sorted_and_never_calls_header_sort(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "input"
            log_dir = Path(tmpdir) / "logs"
            root.mkdir()
            log_dir.mkdir()
            for rel in ("c/3.dcm", "a/1.dcm", "b/2.dcm"):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            chosen = [root / "c" / "3.dcm", root / "a" / "1.dcm"]
            with patch.object(
                run_echodeid, "random"
            ) as mock_random, patch.object(
                run_echodeid,
                "load_done_src_paths_from_worker_logs",
                return_value=set(),
            ), patch.object(
                run_echodeid.pydicom,
                "dcmread",
                side_effect=AssertionError("startup should not read headers"),
            ):
                mock_random.sample.return_value = chosen
                dcm_files, total_files, done_src, todo = run_echodeid.prepare_todo_files(
                    root, log_dir, subsample=2
                )

        self.assertEqual(total_files, 3)
        self.assertEqual(done_src, set())
        self.assertEqual(
            [str(p.relative_to(root)) for p in dcm_files],
            ["a/1.dcm", "c/3.dcm"],
        )
        self.assertEqual(
            [str(Path(p).relative_to(root)) for p in todo],
            ["a/1.dcm", "c/3.dcm"],
        )


if __name__ == "__main__":
    unittest.main()
