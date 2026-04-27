import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from deidecho_run import run_echodeid


class _FakeWritableDataset:
    def save_as(self, path):
        return None


def _make_baseline_dataset():
    return SimpleNamespace(
        file_meta=SimpleNamespace(TransferSyntaxUID=run_echodeid.JPEG_BASELINE_TSUID),
        Rows=4,
        Columns=4,
        NumberOfFrames="1",
    )


class TestRunEchoDeidJpegBackend(unittest.TestCase):
    def test_python_only_skips_pixelmed_and_uses_python_backend(self):
        ds = _make_baseline_dataset()
        results = {"flagged": True, "results": [{"coordinates": [[0, "all"]]}]}

        with tempfile.TemporaryDirectory() as tmpdir:
            dcm_path = tempfile.NamedTemporaryFile(dir=tmpdir, suffix=".dcm", delete=False)
            dcm_path.close()
            with patch.object(run_echodeid.pydicom, "dcmread", return_value=ds), patch.object(
                run_echodeid,
                "redact_encapsulated_baseline_jpeg_frames_pixelmed",
                side_effect=AssertionError("PixelMed should not be used"),
            ), patch.object(
                run_echodeid,
                "redact_python_jpeg_baseline_frames",
                return_value=_FakeWritableDataset(),
            ):
                ok, err, codec = run_echodeid.jpeg_baseline_redact_overwrite_inplace(
                    dcm_path.name,
                    results,
                    run_echodeid.JPEG_BASELINE_BACKEND_PYTHON_ONLY,
                )

        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(codec, "python_jpeg_baseline")

    def test_auto_falls_back_to_python_and_preserves_pixelmed_reason(self):
        ds = _make_baseline_dataset()
        results = {"flagged": True, "results": [{"coordinates": [[0, "all"]]}]}

        with tempfile.TemporaryDirectory() as tmpdir:
            dcm_path = tempfile.NamedTemporaryFile(dir=tmpdir, suffix=".dcm", delete=False)
            dcm_path.close()
            with patch.object(run_echodeid.pydicom, "dcmread", return_value=ds), patch.object(
                run_echodeid,
                "redact_encapsulated_baseline_jpeg_frames_pixelmed",
                side_effect=run_echodeid.PixelMedUnavailableError("javac not found on PATH"),
            ), patch.object(
                run_echodeid,
                "redact_python_jpeg_baseline_frames",
                return_value=_FakeWritableDataset(),
            ):
                run_echodeid._PIXELMED_AUTO_UNAVAILABLE_ERROR = ""
                ok, err, codec = run_echodeid.jpeg_baseline_redact_overwrite_inplace(
                    dcm_path.name,
                    results,
                    run_echodeid.JPEG_BASELINE_BACKEND_AUTO,
                )

        self.assertTrue(ok)
        self.assertEqual(codec, "python_jpeg_baseline")
        self.assertIn("pixelmed_jpeg_baseline_unavailable", err)
        self.assertIn("javac not found on PATH", err)

    def test_require_pixelmed_returns_failure_without_python_fallback(self):
        ds = _make_baseline_dataset()
        results = {"flagged": True, "results": [{"coordinates": [[0, "all"]]}]}

        with tempfile.TemporaryDirectory() as tmpdir:
            dcm_path = tempfile.NamedTemporaryFile(dir=tmpdir, suffix=".dcm", delete=False)
            dcm_path.close()
            with patch.object(run_echodeid.pydicom, "dcmread", return_value=ds), patch.object(
                run_echodeid,
                "redact_encapsulated_baseline_jpeg_frames_pixelmed",
                side_effect=run_echodeid.PixelMedUnavailableError("java not found on PATH"),
            ), patch.object(
                run_echodeid,
                "redact_python_jpeg_baseline_frames",
                side_effect=AssertionError("Python fallback should not be used"),
            ):
                ok, err, codec = run_echodeid.jpeg_baseline_redact_overwrite_inplace(
                    dcm_path.name,
                    results,
                    run_echodeid.JPEG_BASELINE_BACKEND_REQUIRE_PIXELMED,
                )

        self.assertFalse(ok)
        self.assertEqual(codec, "")
        self.assertIn("pixelmed_jpeg_baseline_unavailable", err)
        self.assertIn("java not found on PATH", err)


if __name__ == "__main__":
    unittest.main()
