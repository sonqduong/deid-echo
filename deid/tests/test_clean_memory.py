#!/usr/bin/env python

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy

from deid.dicom.pixels.clean import clean_pixel_data


def _make_fake_dicom(pixel_array, photometric="MONOCHROME2", samples_per_pixel=1):
    return SimpleNamespace(
        PixelData=pixel_array.tobytes(),
        PhotometricInterpretation=photometric,
        SamplesPerPixel=samples_per_pixel,
        pixel_array=pixel_array,
        file_meta=SimpleNamespace(
            TransferSyntaxUID=SimpleNamespace(is_compressed=False)
        ),
    )


def _results_for_box(xmin=1, ymin=1, xmax=3, ymax=3):
    return {
        "flagged": True,
        "results": [{"coordinates": [(0, [[xmin, ymin, xmax, ymax]])]}],
    }


class TestCleanPixelMemoryPaths(unittest.TestCase):
    def _run_clean(self, dicom, results, expected_length=None):
        if expected_length is None:
            expected_length = len(dicom.PixelData)
        with patch(
            "deid.dicom.pixels.clean.utils.load_dicom", return_value=dicom
        ), patch(
            "deid.dicom.pixels.clean.get_expected_length",
            return_value=expected_length,
        ):
            return clean_pixel_data(dicom_file="ignored.dcm", results=results)

    def test_clean_pixel_data_2d_grayscale_broadcasts_mask(self):
        original = numpy.arange(20, dtype=numpy.uint16).reshape(4, 5)
        dicom = _make_fake_dicom(original)

        cleaned = self._run_clean(dicom, _results_for_box())
        expected = original.copy()
        expected[1:3, 1:3] = 0

        numpy.testing.assert_array_equal(cleaned, expected)

    def test_clean_pixel_data_3d_rgb_image_broadcasts_mask(self):
        original = numpy.arange(4 * 5 * 3, dtype=numpy.uint8).reshape(4, 5, 3)
        dicom = _make_fake_dicom(original, photometric="RGB", samples_per_pixel=3)

        cleaned = self._run_clean(dicom, _results_for_box())
        expected = original.copy()
        expected[1:3, 1:3, :] = 0

        numpy.testing.assert_array_equal(cleaned, expected)

    def test_clean_pixel_data_3d_grayscale_cine_broadcasts_mask(self):
        original = numpy.arange(2 * 4 * 5, dtype=numpy.uint16).reshape(2, 4, 5)
        dicom = _make_fake_dicom(original)

        cleaned = self._run_clean(dicom, _results_for_box())
        expected = original.copy()
        expected[:, 1:3, 1:3] = 0

        numpy.testing.assert_array_equal(cleaned, expected)

    def test_clean_pixel_data_4d_rgb_cine_broadcasts_mask(self):
        original = numpy.arange(2 * 4 * 5 * 3, dtype=numpy.uint8).reshape(
            2, 4, 5, 3
        )
        dicom = _make_fake_dicom(original, photometric="RGB", samples_per_pixel=3)

        cleaned = self._run_clean(dicom, _results_for_box())
        expected = original.copy()
        expected[:, 1:3, 1:3, :] = 0

        numpy.testing.assert_array_equal(cleaned, expected)

    def test_clean_pixel_data_palette_color_multiframe_masks_rgb_output(self):
        original = numpy.arange(2 * 4 * 5, dtype=numpy.uint8).reshape(2, 4, 5)
        dicom = _make_fake_dicom(
            original, photometric="PALETTE COLOR", samples_per_pixel=1
        )

        def fake_apply_color_lut(frame, _dicom):
            return numpy.stack((frame, frame + 1, frame + 2), axis=-1)

        with patch(
            "deid.dicom.pixels.clean.apply_color_lut", side_effect=fake_apply_color_lut
        ):
            cleaned = self._run_clean(dicom, _results_for_box())

        expected = numpy.stack(
            [fake_apply_color_lut(frame, dicom) for frame in original], axis=0
        )
        expected[:, 1:3, 1:3, :] = 0

        numpy.testing.assert_array_equal(cleaned, expected)

    def test_clean_pixel_data_does_not_use_numpy_tile_for_4d_rgb(self):
        original = numpy.arange(2 * 4 * 5 * 3, dtype=numpy.uint8).reshape(
            2, 4, 5, 3
        )
        dicom = _make_fake_dicom(original, photometric="RGB", samples_per_pixel=3)

        with patch(
            "deid.dicom.pixels.clean.numpy.tile",
            side_effect=AssertionError("numpy.tile should not be used"),
        ):
            cleaned = self._run_clean(dicom, _results_for_box())

        expected = original.copy()
        expected[:, 1:3, 1:3, :] = 0
        numpy.testing.assert_array_equal(cleaned, expected)


if __name__ == "__main__":
    unittest.main()
