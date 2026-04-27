import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pydicom
from pydicom.encaps import generate_frames

from deid.dicom.pixels.clean import build_mask_from_results

try:
    import pixelmed_jpeg_redaction as pixelmed_mod
    from pixelmed_jpeg_redaction import (
        inspect_pixelmed_runtime,
        mask_to_redaction_rectangles,
        pixelmed_bridge_available,
        redact_baseline_jpeg_bytes_pixelmed,
    )
except ImportError:
    from deidecho_run import pixelmed_jpeg_redaction as pixelmed_mod
    from deidecho_run.pixelmed_jpeg_redaction import (
        inspect_pixelmed_runtime,
        mask_to_redaction_rectangles,
        pixelmed_bridge_available,
        redact_baseline_jpeg_bytes_pixelmed,
    )


JPEG_BASELINE_TSUID = "1.2.840.10008.1.2.4.50"


class TestPixelMedMaskRectangles(unittest.TestCase):
    def test_top_band_mask_becomes_one_exact_rectangle(self):
        redact_mask = np.zeros((10, 8), dtype=bool)
        redact_mask[:3, :] = True

        self.assertEqual(
            mask_to_redaction_rectangles(redact_mask),
            [(0, 0, 8, 3)],
        )

    def test_keep_area_to_image_height_does_not_redact_bottom_row(self):
        results = {
            "flagged": True,
            "results": [
                {
                    "coordinates": [
                        [0, "all"],
                        [1, "0,3,8,10"],
                    ]
                }
            ],
        }
        keep_mask = build_mask_from_results(results, rows=10, columns=8)
        redact_mask = keep_mask == 0

        self.assertFalse(redact_mask[-1, :].any())
        self.assertEqual(
            mask_to_redaction_rectangles(redact_mask),
            [(0, 0, 8, 3)],
        )

    def test_disjoint_runs_stay_exact(self):
        redact_mask = np.zeros((5, 6), dtype=bool)
        redact_mask[0:2, 1:3] = True
        redact_mask[3:5, 4:6] = True

        self.assertEqual(
            sorted(mask_to_redaction_rectangles(redact_mask)),
            [(1, 0, 2, 2), (4, 3, 2, 2)],
        )


def _first_testbatch_baseline_jpeg_frame():
    root = Path(__file__).resolve().parents[2] / "test" / "data" / "testbatch"
    if not root.is_dir():
        return None

    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            header = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        except Exception:
            continue
        tsuid = str(getattr(getattr(header, "file_meta", None), "TransferSyntaxUID", ""))
        if tsuid != JPEG_BASELINE_TSUID:
            continue
        ds = pydicom.dcmread(str(path), force=True)
        frames = generate_frames(
            ds.PixelData,
            number_of_frames=int(getattr(ds, "NumberOfFrames", 1) or 1),
        )
        return next(frames)
    return None


class TestPixelMedJavaBridge(unittest.TestCase):
    @unittest.skipUnless(
        pixelmed_bridge_available(),
        "PixelMed bridge requires pixelmed_codec.jar plus java and javac",
    )
    def test_pixelmed_bridge_round_trips_a_baseline_frame(self):
        frame = _first_testbatch_baseline_jpeg_frame()
        if frame is None:
            self.skipTest("No JPEG Baseline frame found in test/data/testbatch")

        redacted = redact_baseline_jpeg_bytes_pixelmed(frame, [])
        self.assertTrue(redacted.startswith(b"\xff\xd8"))
        self.assertTrue(redacted.endswith(b"\xff\xd9"))


class TestPixelMedDiagnostics(unittest.TestCase):
    def test_inspect_pixelmed_runtime_reports_missing_java(self):
        with patch.object(
            pixelmed_mod, "resolve_pixelmed_codec_jar", return_value=Path("/tmp/pixelmed_codec.jar")
        ), patch.object(
            pixelmed_mod,
            "_resolve_executable",
            side_effect=pixelmed_mod.PixelMedUnavailableError("java not found on PATH"),
        ):
            diagnostics = inspect_pixelmed_runtime()

        self.assertFalse(diagnostics["available"])
        self.assertEqual(diagnostics["jar_path"], "/tmp/pixelmed_codec.jar")
        self.assertEqual(diagnostics["error"], "java not found on PATH")

    def test_inspect_pixelmed_runtime_reports_missing_javac(self):
        with patch.object(
            pixelmed_mod, "resolve_pixelmed_codec_jar", return_value=Path("/tmp/pixelmed_codec.jar")
        ), patch.object(
            pixelmed_mod,
            "_resolve_executable",
            side_effect=["/usr/bin/java", pixelmed_mod.PixelMedUnavailableError("javac not found on PATH")],
        ):
            diagnostics = inspect_pixelmed_runtime()

        self.assertFalse(diagnostics["available"])
        self.assertEqual(diagnostics["java_path"], "/usr/bin/java")
        self.assertEqual(diagnostics["error"], "javac not found on PATH")

    def test_inspect_pixelmed_runtime_reports_missing_jar(self):
        with patch.object(
            pixelmed_mod,
            "resolve_pixelmed_codec_jar",
            side_effect=pixelmed_mod.PixelMedUnavailableError("PixelMed codec jar not found"),
        ):
            diagnostics = inspect_pixelmed_runtime()

        self.assertFalse(diagnostics["available"])
        self.assertEqual(diagnostics["error"], "PixelMed codec jar not found")

    def test_inspect_pixelmed_runtime_reports_compile_failure(self):
        with patch.object(
            pixelmed_mod, "resolve_pixelmed_codec_jar", return_value=Path("/tmp/pixelmed_codec.jar")
        ), patch.object(
            pixelmed_mod,
            "_resolve_executable",
            side_effect=["/usr/bin/java", "/usr/bin/javac"],
        ), patch.object(
            pixelmed_mod,
            "compile_pixelmed_bridge",
            side_effect=pixelmed_mod.PixelMedUnavailableError(
                "Could not compile PixelMed bridge: bad javac"
            ),
        ):
            diagnostics = inspect_pixelmed_runtime()

        self.assertFalse(diagnostics["available"])
        self.assertEqual(diagnostics["javac_path"], "/usr/bin/javac")
        self.assertIn("Could not compile PixelMed bridge", diagnostics["error"])


if __name__ == "__main__":
    unittest.main()
