import unittest
from pathlib import Path

import numpy as np
import pydicom
from pydicom.encaps import generate_frames

from deid.dicom.pixels.clean import build_mask_from_results

try:
    from pixelmed_jpeg_redaction import (
        mask_to_redaction_rectangles,
        pixelmed_bridge_available,
        redact_baseline_jpeg_bytes_pixelmed,
    )
except ImportError:
    from deidecho_run.pixelmed_jpeg_redaction import (
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


if __name__ == "__main__":
    unittest.main()
