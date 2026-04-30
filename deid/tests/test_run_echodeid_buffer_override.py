import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deidecho_run import run_echodeid


class _FakeParser:
    def __init__(self, dicom_file, recipe=None, dicom=None):
        self.dicom_file = dicom_file
        self.recipe = recipe
        self.dicom = dicom

    def parse(self, remove_private=True):
        return None

    def save(self, filename, overwrite=True):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        Path(filename).write_bytes(b"header")


class _FakeCleaner:
    detect_calls = []
    clean_called = False
    saved_paths = []

    def __init__(self, output_folder=None, deid=None):
        self.output_folder = output_folder
        self.deid = deid
        self.results = {
            "flagged": True,
            "results": [{"coordinates": [[0, "all"], [1, "0,1,4,4"]]}],
        }

    def detect(self, path, mask_above_top=False, buffer_pct=0.0):
        _FakeCleaner.detect_calls.append(
            {
                "path": path,
                "mask_above_top": mask_above_top,
                "buffer_pct": buffer_pct,
            }
        )
        return self.results

    def clean(self):
        _FakeCleaner.clean_called = True

    def save_dicom(self, filename=None, jpeg_ls=False):
        out_path = Path(self.output_folder) / filename
        out_path.write_bytes(b"pixel")
        _FakeCleaner.saved_paths.append(str(out_path))
        return str(out_path)


def _make_dataset(tsuid):
    return SimpleNamespace(
        file_meta=SimpleNamespace(TransferSyntaxUID=tsuid),
        PhotometricInterpretation="RGB",
        PlanarConfiguration=0,
        Manufacturer="ACUSON",
        ManufacturerModelName="SEQUOIA",
        SoftwareVersions="1",
        Modality="US",
        Rows=4,
        Columns=4,
        NumberOfFrames="2",
        SequenceOfUltrasoundRegions=[object()],
        PatientID="PATIENT1",
        StudyDate="20250101",
        StudyTime="010203",
        StudyInstanceUID="1.2.3.4",
        SeriesInstanceUID="1.2.3.4.5",
        SOPInstanceUID="1.2.3.4.5.6",
        SOPClassUID=next(iter(run_echodeid.ALLOWED_SOP)),
        InstanceNumber="7",
        SeriesNumber="1",
    )


def _fake_clean_pixel_data_to_file(_dicom_file, _results, output_file, **_kwargs):
    Path(output_file).write_bytes(b"pixel")
    _FakeCleaner.clean_called = True
    return str(output_file)


class TestRunEchoDeidBufferOverride(unittest.TestCase):
    def setUp(self):
        _FakeCleaner.detect_calls = []
        _FakeCleaner.clean_called = False
        _FakeCleaner.saved_paths = []

    def test_parse_args_omitted_buffer_pct_is_none(self):
        args = run_echodeid.parse_args(
            [
                "--input-root",
                "/tmp/in",
                "--output-root",
                "/tmp/out",
                "--recipe-path",
                "/tmp/recipe",
            ]
        )
        self.assertIsNone(args.buffer_pct)

    def test_parse_args_defaults_flush_every_to_100(self):
        args = run_echodeid.parse_args(
            [
                "--input-root",
                "/tmp/in",
                "--output-root",
                "/tmp/out",
                "--recipe-path",
                "/tmp/recipe",
            ]
        )
        self.assertEqual(args.flush_every, 100)

    def test_parse_args_defaults_workers_to_5(self):
        args = run_echodeid.parse_args(
            [
                "--input-root",
                "/tmp/in",
                "--output-root",
                "/tmp/out",
                "--recipe-path",
                "/tmp/recipe",
            ]
        )
        self.assertEqual(args.workers, 5)

    def test_parse_args_defaults_pixelmed_frame_batch_size_to_32(self):
        args = run_echodeid.parse_args(
            [
                "--input-root",
                "/tmp/in",
                "--output-root",
                "/tmp/out",
                "--recipe-path",
                "/tmp/recipe",
            ]
        )
        self.assertEqual(args.pixelmed_frame_batch_size, 32)

    def test_parse_args_accepts_valid_buffer_pct_values(self):
        for value in ("0", "0.02", "0.5", "1.0"):
            args = run_echodeid.parse_args(
                [
                    "--input-root",
                    "/tmp/in",
                    "--output-root",
                    "/tmp/out",
                    "--recipe-path",
                    "/tmp/recipe",
                    "--buffer-pct",
                    value,
                ]
            )
            self.assertEqual(run_echodeid.validate_buffer_pct(args.buffer_pct), float(value))

    def test_validate_buffer_pct_rejects_out_of_range_values(self):
        for value in (-0.01, 1.1):
            with self.assertRaises(ValueError):
                run_echodeid.validate_buffer_pct(value)

    def test_resolve_buffer_pct_cli_override_wins(self):
        self.assertEqual(
            run_echodeid.resolve_buffer_pct(0.02, "ACUSON", "SEQUOIA"),
            0.02,
        )

    def test_resolve_pixelmed_concurrency_defaults_to_worker_count(self):
        self.assertEqual(run_echodeid.resolve_pixelmed_concurrency(None, 10), 10)

    def test_resolve_pixelmed_concurrency_uses_smaller_explicit_value(self):
        self.assertEqual(run_echodeid.resolve_pixelmed_concurrency(3, 10), 3)

    def test_resolve_pixelmed_concurrency_caps_to_worker_count(self):
        self.assertEqual(run_echodeid.resolve_pixelmed_concurrency(20, 10), 10)

    def test_process_one_cli_override_bypasses_table_lookup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ds = _make_dataset("1.2.840.10008.1.2.1")
            out_root = Path(tmpdir) / "out"
            log_dir = Path(tmpdir) / "logs"
            out_root.mkdir()
            log_dir.mkdir()

            parser = _FakeParser("ignored", dicom=ds)
            with patch.object(run_echodeid, "read_dicom_metadata", return_value=ds), patch.object(
                run_echodeid, "DicomParser", return_value=parser
            ), patch.object(
                run_echodeid, "DicomCleaner", _FakeCleaner
            ), patch.object(
                run_echodeid, "extract_region_spatial_formats", return_value=[1]
            ), patch.object(
                run_echodeid,
                "clean_pixel_data_to_file",
                side_effect=_fake_clean_pixel_data_to_file,
            ), patch.object(
                run_echodeid, "append_row_to_worker_csv"
            ), patch.object(
                run_echodeid, "get_buffer_pct", side_effect=AssertionError("should not be used")
            ), patch.object(
                run_echodeid, "read_transfer_syntax_uid", side_effect=["1.2.840.10008.1.2.1", "1.2.840.10008.1.2.1"]
            ):
                row = run_echodeid.process_one(
                    "input.dcm",
                    0,
                    tmpdir,
                    str(out_root),
                    "/tmp/recipe",
                    str(log_dir),
                    run_echodeid.JPEG_BASELINE_BACKEND_AUTO,
                    0.25,
                    run_echodeid.ALLOWED_SOP,
                    run_echodeid.ALLOWED_RSF,
                    run_echodeid.TRAITS,
                    run_echodeid.FINAL_COLUMNS,
                )

        self.assertEqual(_FakeCleaner.detect_calls[0]["buffer_pct"], 0.25)
        self.assertTrue(_FakeCleaner.clean_called)
        self.assertEqual(row["status"], "success")
        self.assertIn("/deidentified/", row["header_path"])

    def test_process_one_uses_cli_override_for_jpeg_baseline_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ds = _make_dataset(run_echodeid.JPEG_BASELINE_TSUID)
            out_root = Path(tmpdir) / "out"
            log_dir = Path(tmpdir) / "logs"
            out_root.mkdir()
            log_dir.mkdir()

            parser = _FakeParser("ignored", dicom=ds)
            with patch.object(run_echodeid, "read_dicom_metadata", return_value=ds), patch.object(
                run_echodeid, "DicomParser", return_value=parser
            ), patch.object(
                run_echodeid, "DicomCleaner", _FakeCleaner
            ), patch.object(
                run_echodeid, "extract_region_spatial_formats", return_value=[1]
            ), patch.object(
                run_echodeid,
                "clean_pixel_data_to_file",
                side_effect=_fake_clean_pixel_data_to_file,
            ), patch.object(
                run_echodeid, "append_row_to_worker_csv"
            ), patch.object(
                run_echodeid, "jpeg_baseline_redact_overwrite_inplace", return_value=(False, "boom", "")
            ), patch.object(
                run_echodeid, "read_transfer_syntax_uid", side_effect=[run_echodeid.JPEG_BASELINE_TSUID, run_echodeid.JPEG_BASELINE_TSUID]
            ):
                row = run_echodeid.process_one(
                    "input.dcm",
                    0,
                    tmpdir,
                    str(out_root),
                    "/tmp/recipe",
                    str(log_dir),
                    run_echodeid.JPEG_BASELINE_BACKEND_AUTO,
                    0.02,
                    run_echodeid.ALLOWED_SOP,
                    run_echodeid.ALLOWED_RSF,
                    run_echodeid.TRAITS,
                    run_echodeid.FINAL_COLUMNS,
                )

        self.assertEqual(_FakeCleaner.detect_calls[0]["buffer_pct"], 0.02)
        self.assertTrue(_FakeCleaner.clean_called)
        self.assertEqual(row["pixel_redaction_method"], "decompressed_fallback")
        self.assertEqual(row["status"], "success")

    def test_process_one_require_pixelmed_does_not_fallback_to_decompressed_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ds = _make_dataset(run_echodeid.JPEG_BASELINE_TSUID)
            out_root = Path(tmpdir) / "out"
            log_dir = Path(tmpdir) / "logs"
            out_root.mkdir()
            log_dir.mkdir()

            parser = _FakeParser("ignored", dicom=ds)
            with patch.object(run_echodeid, "read_dicom_metadata", return_value=ds), patch.object(
                run_echodeid, "DicomParser", return_value=parser
            ), patch.object(
                run_echodeid, "DicomCleaner", _FakeCleaner
            ), patch.object(
                run_echodeid, "extract_region_spatial_formats", return_value=[1]
            ), patch.object(
                run_echodeid, "append_row_to_worker_csv"
            ), patch.object(
                run_echodeid,
                "jpeg_baseline_redact_overwrite_inplace",
                return_value=(False, "pixelmed unavailable", ""),
            ), patch.object(
                run_echodeid, "read_transfer_syntax_uid", return_value=run_echodeid.JPEG_BASELINE_TSUID
            ):
                row = run_echodeid.process_one(
                    "input.dcm",
                    0,
                    tmpdir,
                    str(out_root),
                    "/tmp/recipe",
                    str(log_dir),
                    run_echodeid.JPEG_BASELINE_BACKEND_REQUIRE_PIXELMED,
                    0.02,
                    run_echodeid.ALLOWED_SOP,
                    run_echodeid.ALLOWED_RSF,
                    run_echodeid.TRAITS,
                    run_echodeid.FINAL_COLUMNS,
                )

        self.assertFalse(_FakeCleaner.clean_called)
        self.assertEqual(row["status"], "pixel_fail")
        self.assertEqual(row["pixel_redaction_method"], "require_pixelmed")
        self.assertEqual(row["compressed_redaction_error"], "pixelmed unavailable")

    def test_process_one_without_cli_override_uses_table_lookup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ds = _make_dataset("1.2.840.10008.1.2.1")
            out_root = Path(tmpdir) / "out"
            log_dir = Path(tmpdir) / "logs"
            out_root.mkdir()
            log_dir.mkdir()

            parser = _FakeParser("ignored", dicom=ds)
            with patch.object(run_echodeid, "read_dicom_metadata", return_value=ds), patch.object(
                run_echodeid, "DicomParser", return_value=parser
            ), patch.object(
                run_echodeid, "DicomCleaner", _FakeCleaner
            ), patch.object(
                run_echodeid, "extract_region_spatial_formats", return_value=[1]
            ), patch.object(
                run_echodeid,
                "clean_pixel_data_to_file",
                side_effect=_fake_clean_pixel_data_to_file,
            ), patch.object(
                run_echodeid, "append_row_to_worker_csv"
            ), patch.object(
                run_echodeid, "get_buffer_pct", return_value=0.77
            ), patch.object(
                run_echodeid, "read_transfer_syntax_uid", side_effect=["1.2.840.10008.1.2.1", "1.2.840.10008.1.2.1"]
            ):
                run_echodeid.process_one(
                    "input.dcm",
                    0,
                    tmpdir,
                    str(out_root),
                    "/tmp/recipe",
                    str(log_dir),
                    run_echodeid.JPEG_BASELINE_BACKEND_AUTO,
                    None,
                    run_echodeid.ALLOWED_SOP,
                    run_echodeid.ALLOWED_RSF,
                    run_echodeid.TRAITS,
                    run_echodeid.FINAL_COLUMNS,
                )

        self.assertEqual(_FakeCleaner.detect_calls[0]["buffer_pct"], 0.77)

    def test_process_one_skips_disallowed_sop_before_full_parser_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ds = _make_dataset("1.2.840.10008.1.2.1")
            ds.SOPClassUID = "9.9.9"
            out_root = Path(tmpdir) / "out"
            log_dir = Path(tmpdir) / "logs"
            out_root.mkdir()
            log_dir.mkdir()

            with patch.object(
                run_echodeid, "read_dicom_metadata", return_value=ds
            ), patch.object(
                run_echodeid,
                "DicomParser",
                side_effect=AssertionError("full parser should not run for skipped SOP"),
            ), patch.object(
                run_echodeid, "append_row_to_worker_csv"
            ):
                row = run_echodeid.process_one(
                    "input.dcm",
                    0,
                    tmpdir,
                    str(out_root),
                    "/tmp/recipe",
                    str(log_dir),
                    run_echodeid.JPEG_BASELINE_BACKEND_AUTO,
                    None,
                    run_echodeid.ALLOWED_SOP,
                    run_echodeid.ALLOWED_RSF,
                    run_echodeid.TRAITS,
                    run_echodeid.FINAL_COLUMNS,
                )

        self.assertEqual(row["status"], "skipped_disallowed_sop")


if __name__ == "__main__":
    unittest.main()
