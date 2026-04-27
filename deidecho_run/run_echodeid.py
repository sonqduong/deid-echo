import argparse
import hashlib
import multiprocessing as mp
import os
import platform
import random
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pydicom

try:
    from deid_helpers import (
        append_row_to_worker_csv,
        as_int_or_none,
        extract_region_spatial_formats,
        fmt3,
        fmt5,
        formats_to_str,
        load_done_src_paths_from_worker_logs,
        rebuild_master_log,
        safe_getattr,
        sanitize_for_path,
    )
except ImportError:
    from deidecho_run.deid_helpers import (
        append_row_to_worker_csv,
        as_int_or_none,
        extract_region_spatial_formats,
        fmt3,
        fmt5,
        formats_to_str,
        load_done_src_paths_from_worker_logs,
        rebuild_master_log,
        safe_getattr,
        sanitize_for_path,
    )

from deid.dicom.parser import DicomParser
from deid.dicom.pixels.clean import DicomCleaner, build_mask_from_results

try:
    from jpeg_selective_redaction import (
        redact_encapsulated_baseline_jpeg_frames as redact_python_jpeg_baseline_frames,
    )
except ImportError:
    from deidecho_run.jpeg_selective_redaction import (
        redact_encapsulated_baseline_jpeg_frames as redact_python_jpeg_baseline_frames,
    )

try:
    from pixelmed_jpeg_redaction import (
        PixelMedUnavailableError,
        inspect_pixelmed_runtime,
        mask_to_redaction_rectangles,
        redact_encapsulated_baseline_jpeg_frames_pixelmed,
    )
except ImportError:
    from deidecho_run.pixelmed_jpeg_redaction import (
        PixelMedUnavailableError,
        inspect_pixelmed_runtime,
        mask_to_redaction_rectangles,
        redact_encapsulated_baseline_jpeg_frames_pixelmed,
    )

# ---------------- CONSTANTS ----------------
JPEG_BASELINE_TSUID = "1.2.840.10008.1.2.4.50"
_JPEG_REDACTION_PLAN_CACHE: Dict[Tuple[Any, ...], Any] = {}
_PIXELMED_AUTO_UNAVAILABLE_ERROR: str = ""

JPEG_BASELINE_BACKEND_AUTO = "auto"
JPEG_BASELINE_BACKEND_REQUIRE_PIXELMED = "require-pixelmed"
JPEG_BASELINE_BACKEND_PYTHON_ONLY = "python-only"
JPEG_BASELINE_BACKEND_CHOICES = (
    JPEG_BASELINE_BACKEND_AUTO,
    JPEG_BASELINE_BACKEND_REQUIRE_PIXELMED,
    JPEG_BASELINE_BACKEND_PYTHON_ONLY,
)

ALLOWED_SOP: Set[str] = {
    "1.2.840.10008.5.1.4.1.1.3.1",  # US multi-frame
    "1.2.840.10008.5.1.4.1.1.6.1",  # Ultrasound image
}

ALLOWED_RSF: Set[int] = {1, 2, 3}  # allowed RegionSpatialFormat values

TRAITS = [
    ("PatientID", "PatientID"),
    ("StudyDate", "StudyDate"),
    ("StudyTime", "StudyTime"),
    ("StudyInstanceUID", "StudyInstanceUID"),
    ("SeriesInstanceUID", "SeriesInstanceUID"),
    ("SOPInstanceUID", "SOPInstanceUID"),
    ("SOPClassUID", "SOPClassUID"),
    ("InstanceNumber", "InstanceNumber"),
    ("SeriesNumber", "SeriesNumber"),
]

FINAL_COLUMNS = [
    "input_root",
    "src_path",
    "secret_salt",
    "TransferSyntaxUID_before",
    "PhotometricInterpretation_before",
    "PlanarConfiguration_before",
    "PatientID_before",
    "StudyDate_before",
    "StudyTime_before",
    "StudyInstanceUID_before",
    "SeriesInstanceUID_before",
    "SOPInstanceUID_before",
    "SOPClassUID_before",
    "InstanceNumber_before",
    "has_sequence_of_ultrasound_regions",
    "region_spatial_formats",
    "region_spatial_formats_allowed",
    "Manufacturer",
    "ManufacturerModelName",
    "SoftwareVersions",
    "Rows",
    "Columns",
    "NumberOfFrames",
    # --- AFTER values  ---
    "PatientID",
    "StudyDate",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "Modality",
    "header_path",
    "header_success",
    "dcmtk_rewrite_attempted",
    "dcmtk_rewrite_success",
    "size_after_header",
    "patient_dir",
    "study_dir",
    "series_dir",
    "instance_filename",
    "pixel_path",
    "pixel_success",
    "size_after_pixel",
    "TransferSyntaxUID_after_pixel",
    "detect_s",
    "compressed_redaction_s",
    "clean_s",
    "save_s",
    "pixel_total_s",
    "compressed_redaction_attempted",
    "compressed_redaction_success",
    "compressed_redaction_codec",
    "compressed_redaction_error",
    "compressed_redaction_plan_cache_size",
    "pixel_redaction_method",
    "overwritten",
    "status",
    "error",
]

# ---------------- Dictionary of Top Mask overrides beyond upper bounding box ----------------
# Keyed by (Manufacturer, ManufacturerModelName), defaults to 1%

BUFFER_PCT_DEFAULT = 0.01

BUFFER_PCT_BY_MFG_MODEL: Dict[Tuple[str, str], float] = {
    ("ACUSON", "CYPRESS"): 0.10,
    ("GE HEALTHCARE", "VIVID I"): 0.07,
    ("GE ULTRASOUND", "VIVID T8"): 0.07,
    ("GE VINGMED ULTRASOUND", "VIVID E9"): 0.06,
    ("GE VINGMED ULTRASOUND", "VIVID E95"): 0.05,
    ("GE VINGMED ULTRASOUND", "VIVID T9"): 0.05,
    ("GE VINGMED ULTRASOUND", "VIVID7"): 0.07,
    ("GEMS ULTRASOUND", "VIVID7"): 0.08,
    ("GEMS ULTRASOUND", "VIVID I"): 0.06,
    ("GEMS ULTRASOUND", "VIVID Q"): 0.06,
    ("ACUSON", "SEQUOIA"): 0.50,
    # add more:
    # Use wildcards i.e ("*", "*") as a global fallback or specific manufacturer.
    # case insensitive
    # examples:
    # ("PHILIPS", "IE33"): 0.02,
    # ("GE MEDICAL SYSTEMS", "VIVID E95"): 0.03,
}


def _norm_tag(x: Any) -> str:
    return str(x or "").strip().upper()


def get_buffer_pct(manufacturer: Any, model: Any) -> float:
    """
    Resolution order:
      1) exact (MFG, MODEL)
      2) manufacturer wildcard (MFG, "*")
      3) global wildcard ("*", "*")
      4) default
    """
    m = _norm_tag(manufacturer)
    mo = _norm_tag(model)

    if (m, mo) in BUFFER_PCT_BY_MFG_MODEL:
        return float(BUFFER_PCT_BY_MFG_MODEL[(m, mo)])
    if (m, "*") in BUFFER_PCT_BY_MFG_MODEL:
        return float(BUFFER_PCT_BY_MFG_MODEL[(m, "*")])
    if ("*", "*") in BUFFER_PCT_BY_MFG_MODEL:
        return float(BUFFER_PCT_BY_MFG_MODEL[("*", "*")])
    return float(BUFFER_PCT_DEFAULT)


def resolve_buffer_pct(
    cli_buffer_pct: Optional[float], manufacturer: Any, model: Any
) -> float:
    """
    Resolve the effective buffer percentage for a file.

    A CLI-provided value overrides all manufacturer/model defaults.
    """
    if cli_buffer_pct is not None:
        return float(cli_buffer_pct)
    return get_buffer_pct(manufacturer, model)


def validate_buffer_pct(buffer_pct: Optional[float]) -> Optional[float]:
    """
    Validate an optional CLI buffer percentage.
    """
    if buffer_pct is None:
        return None
    value = float(buffer_pct)
    if not 0.0 <= value <= 1.0:
        raise ValueError(
            f"--buffer-pct must be between 0.0 and 1.0 inclusive; got {buffer_pct}"
        )
    return value


def validate_jpeg_baseline_backend(backend: str) -> str:
    value = str(backend).strip()
    if value not in JPEG_BASELINE_BACKEND_CHOICES:
        choices = ", ".join(JPEG_BASELINE_BACKEND_CHOICES)
        raise ValueError(
            f"--jpeg-baseline-backend must be one of {choices}; got {backend!r}"
        )
    return value


def assess_jpeg_baseline_backend(backend: str) -> Dict[str, Any]:
    """
    Resolve startup behavior and diagnostics for the selected JPEG backend.
    """
    backend = validate_jpeg_baseline_backend(backend)
    if backend == JPEG_BASELINE_BACKEND_PYTHON_ONLY:
        return {
            "backend": backend,
            "available": False,
            "status": "skipped",
            "message": (
                "JPEG Baseline backend set to python-only; PixelMed preflight skipped."
            ),
            "diagnostics": {
                "available": False,
                "java_path": "",
                "javac_path": "",
                "jar_path": "",
                "class_dir": "",
                "error": "",
            },
        }

    diagnostics = inspect_pixelmed_runtime()
    if diagnostics["available"]:
        return {
            "backend": backend,
            "available": True,
            "status": "available",
            "message": "PixelMed JPEG Baseline backend is available.",
            "diagnostics": diagnostics,
        }

    if backend == JPEG_BASELINE_BACKEND_REQUIRE_PIXELMED:
        message = (
            "PixelMed is required for JPEG Baseline redaction but is unavailable: "
            f"{diagnostics['error']}"
        )
    else:
        message = (
            "PixelMed unavailable for JPEG Baseline redaction; files will fall back "
            f"to python_jpeg_baseline. reason={diagnostics['error']}"
        )

    return {
        "backend": backend,
        "available": False,
        "status": "unavailable",
        "message": message,
        "diagnostics": diagnostics,
    }


# recycle workers every N tasks to limit memory bloat
MAX_TASKS_PER_CHILD = 500

CHUNKSIZE = 8  # used by Pool.imap_unordered; higher values improve plan-cache locality


def worker_log_path(log_dir: Path, worker_id: int) -> Path:
    return log_dir / f"deid_header_pixel_log__worker_{worker_id:03d}.csv"


def _init_worker() -> None:
    """
    Runs once per worker process (and again when a worker is recycled).
    Good place to cap hidden thread pools and reduce memory/CPU contention.
    """
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")


# ---------------- METRICS (ADDED) ----------------
def _metrics_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_metrics_line(
    metrics_path: Path,
    *,
    completed: int,
    total: int,
    run_start_ts: float,
    interval_completed: int,
    interval_start_ts: float,
) -> None:
    """
    Append one metrics line with timestamp + avg files/sec since run start
    AND interval files/sec since last flush, plus elapsed seconds since start.
    """
    now_ts = time.time()

    elapsed_total = now_ts - run_start_ts
    avg_fps = (completed / elapsed_total) if elapsed_total > 0 else 0.0

    elapsed_interval = now_ts - interval_start_ts
    interval_fps = (
        interval_completed / elapsed_interval if elapsed_interval > 0 else 0.0
    )

    line = (
        f"{_metrics_ts()}\t{completed}/{total}\t"
        f"elapsed_s={elapsed_total:.1f}\t"
        f"avg_fps={avg_fps:.3f}\tinterval_fps={interval_fps:.3f}\n"
    )

    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    with open(metrics_path, "a", encoding="utf-8") as f:
        f.write(line)


def dcmtk_decode_overwrite_inplace(dcm_path: Path) -> Tuple[bool, str]:
    """
    Use DCMTK dcmdjpeg to decode JPEG Lossless -> Explicit VR Little Endian,
    writing to a temp file and atomically overwriting the original.
    Returns: (success, error_message)
    """
    dcmdjpeg = shutil.which("dcmdjpeg")
    if not dcmdjpeg:
        return (
            False,
            "dcmdjpeg not found on PATH (dcmtk not installed / env not active)",
        )

    tmp_path = dcm_path.with_suffix(dcm_path.suffix + ".dcmtk_tmp")

    cmd = [
        dcmdjpeg,
        "--write-xfer-little",  # Explicit VR Little Endian
        str(dcm_path),
        str(tmp_path),
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)

    if res.returncode != 0 or not tmp_path.exists():
        stderr = (res.stderr or "").strip()
        stdout = (res.stdout or "").strip()
        msg = f"dcmdjpeg failed rc={res.returncode}"
        if stderr:
            msg += f" | stderr={stderr[:800]}"
        elif stdout:
            msg += f" | stdout={stdout[:800]}"
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False, msg

    # Atomically overwrite original
    try:
        os.replace(str(tmp_path), str(dcm_path))
    except Exception as e:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False, f"os.replace failed: {e!r}"

    return True, ""


def read_transfer_syntax_uid(dcm_path: Path) -> str:
    try:
        ds = pydicom.dcmread(str(dcm_path), stop_before_pixels=True, force=True)
        return str(getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", ""))
    except Exception:
        return ""


def _format_elapsed(start_ts: float) -> str:
    return f"{time.perf_counter() - start_ts:.6f}"


def jpeg_baseline_redact_overwrite_inplace(
    dcm_path: Path,
    results: Dict[str, Any],
    jpeg_baseline_backend: str,
) -> Tuple[bool, str, str]:
    """
    Redact JPEG Baseline encapsulated pixel data without decompressing.

    Returns (success, error_message, codec). Non-JPEG-Baseline files return
    (False, "", "") so callers can choose the normal fallback path without
    logging an error.
    """
    backend = validate_jpeg_baseline_backend(jpeg_baseline_backend)
    ds = pydicom.dcmread(str(dcm_path), force=True)
    tsuid = str(getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", ""))
    if tsuid != JPEG_BASELINE_TSUID:
        return False, "", ""

    rows = int(getattr(ds, "Rows"))
    columns = int(getattr(ds, "Columns"))
    number_of_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)

    keep_mask = build_mask_from_results(results, rows, columns)
    redact_mask = keep_mask == 0
    rectangles = mask_to_redaction_rectangles(redact_mask)
    frame_rectangles = [rectangles] * number_of_frames

    compressed_errors: List[str] = []
    if rectangles:
        global _PIXELMED_AUTO_UNAVAILABLE_ERROR
        if backend == JPEG_BASELINE_BACKEND_PYTHON_ONLY:
            pass
        elif (
            backend == JPEG_BASELINE_BACKEND_AUTO
            and _PIXELMED_AUTO_UNAVAILABLE_ERROR
        ):
            compressed_errors.append(
                "pixelmed_jpeg_baseline_unavailable="
                f"{_PIXELMED_AUTO_UNAVAILABLE_ERROR}"
            )
        else:
            try:
                redacted = redact_encapsulated_baseline_jpeg_frames_pixelmed(
                    ds, frame_rectangles
                )
                redacted.save_as(str(dcm_path))
                return True, "", "pixelmed_jpeg_baseline"
            except PixelMedUnavailableError as e:
                if backend == JPEG_BASELINE_BACKEND_AUTO:
                    _PIXELMED_AUTO_UNAVAILABLE_ERROR = repr(e)
                compressed_errors.append(
                    "pixelmed_jpeg_baseline_unavailable="
                    f"{repr(e) if backend != JPEG_BASELINE_BACKEND_AUTO else _PIXELMED_AUTO_UNAVAILABLE_ERROR}"
                )
                if backend == JPEG_BASELINE_BACKEND_REQUIRE_PIXELMED:
                    return False, " | ".join(compressed_errors), ""
            except Exception as e:
                compressed_errors.append(f"pixelmed_jpeg_baseline={e!r}")
                if backend == JPEG_BASELINE_BACKEND_REQUIRE_PIXELMED:
                    return False, " | ".join(compressed_errors), ""
    else:
        return True, "", "no_pixel_redaction_needed"

    plan_cache_key = (
        rows,
        columns,
        hashlib.sha1(redact_mask.tobytes()).hexdigest(),
    )
    try:
        redacted = redact_python_jpeg_baseline_frames(
            ds,
            [redact_mask] * number_of_frames,
            plan_cache=_JPEG_REDACTION_PLAN_CACHE,
            plan_cache_key=plan_cache_key,
        )
        redacted.save_as(str(dcm_path))
        return True, " | ".join(compressed_errors), "python_jpeg_baseline"
    except Exception as e:
        compressed_errors.append(f"python_jpeg_baseline={e!r}")

    return False, " | ".join(compressed_errors), ""


def discover_dicom_files(input_root: Path) -> List[Path]:
    """
    Return all files under input_root in deterministic lexical path order.
    """
    return sorted(p for p in input_root.rglob("*") if p.is_file())


def prepare_todo_files(
    input_root: Path, log_dir: Path, subsample: Optional[int] = None
) -> Tuple[List[Path], int, Set[str], List[str]]:
    """
    Discover input files, optionally subsample, and apply resume filtering.
    """
    dcm_files = discover_dicom_files(input_root)
    total_files = len(dcm_files)

    if subsample is not None and subsample > 0:
        n = min(subsample, total_files)
        dcm_files = random.sample(dcm_files, n)
        dcm_files = sorted(dcm_files)

    done_src = load_done_src_paths_from_worker_logs(log_dir)
    todo = [str(p) for p in dcm_files if str(p) not in done_src]
    return dcm_files, total_files, done_src, todo


def process_one(
    dcm_path_str: str,
    worker_id: int,
    input_root: str,
    output_root: str,
    recipe_path: str,
    log_dir: str,
    jpeg_baseline_backend: str,
    cli_buffer_pct: Optional[float],
    allowed_sop: Set[str],
    allowed_rsf: Set[int],
    traits: List,
    final_columns: List[str],
) -> Dict[str, Any]:
    """
    Runs in worker process; appends exactly one row to its worker CSV.
    """
    dcm_path = Path(dcm_path_str)
    INPUT_ROOT_ = Path(input_root)
    OUTPUT_ROOT_ = Path(output_root)
    RECIPE_PATH_ = Path(recipe_path)
    LOG_DIR_ = Path(log_dir)

    row: Dict[str, Any] = {
        "input_root": str(INPUT_ROOT_),
        "src_path": str(dcm_path),
        "status": "",
        "error": "",
        "header_path": "",
        "header_success": False,
        "size_after_header": "",
        "patient_dir": "",
        "study_dir": "",
        "series_dir": "",
        "instance_filename": "",
        "pixel_path": "",
        "pixel_success": False,
        "size_after_pixel": "",
        "TransferSyntaxUID_after_pixel": "",
        "detect_s": "",
        "compressed_redaction_s": "",
        "clean_s": "",
        "save_s": "",
        "pixel_total_s": "",
        "compressed_redaction_attempted": False,
        "compressed_redaction_success": False,
        "compressed_redaction_codec": "",
        "compressed_redaction_error": "",
        "compressed_redaction_plan_cache_size": "",
        "pixel_redaction_method": "",
        "overwritten": False,
        "dcmtk_rewrite_success": False,
        "dcmtk_rewrite_attempted": False,
    }

    jpeg_baseline_backend = validate_jpeg_baseline_backend(jpeg_baseline_backend)
    buffer_pct = validate_buffer_pct(cli_buffer_pct)

    out_csv = worker_log_path(LOG_DIR_, worker_id)

    # --- READ ORIGINAL ---
    try:
        parser = DicomParser(str(dcm_path), recipe=str(RECIPE_PATH_))
        ds = parser.dicom
    except Exception as e:
        row.update({"status": "read_error", "error": repr(e)})
        append_row_to_worker_csv(row, out_csv, final_columns)
        return row

    # --- BEFORE HEADER values ---
    row["secret_salt"] = os.getenv("SECRET_SALT", "")
    row["TransferSyntaxUID_before"] = safe_getattr(
        getattr(ds, "file_meta", None),
        "TransferSyntaxUID",
        "",
    )
    row["PhotometricInterpretation_before"] = safe_getattr(
        ds, "PhotometricInterpretation", ""
    )
    row["PlanarConfiguration_before"] = safe_getattr(ds, "PlanarConfiguration", "")
    row["Manufacturer"] = safe_getattr(ds, "Manufacturer", "")
    row["ManufacturerModelName"] = safe_getattr(ds, "ManufacturerModelName", "")
    row["SoftwareVersions"] = safe_getattr(ds, "SoftwareVersions", "")
    row["Modality"] = safe_getattr(ds, "Modality", "")
    row["Rows"] = safe_getattr(ds, "Rows", "")
    row["Columns"] = safe_getattr(ds, "Columns", "")
    row["NumberOfFrames"] = safe_getattr(ds, "NumberOfFrames", "1") or "1"

    # --- BEFORE HEADER (existing traits) ---
    for tag_name, col_name in traits:
        row[f"{col_name}_before"] = safe_getattr(ds, tag_name, "")

    # SOPClassUID filter (BEFORE)
    sop_before = row.get("SOPClassUID_before", "")
    if sop_before not in allowed_sop:
        row.update(
            {
                "status": "skipped_disallowed_sop",
                "header_success": False,
                "pixel_success": False,
                "overwritten": False,
                "has_sequence_of_ultrasound_regions": "",
                "region_spatial_formats": "",
                "region_spatial_formats_allowed": "",
            }
        )
        append_row_to_worker_csv(row, out_csv, final_columns)
        return row

    # --- Ultrasound Regions + RSF filter (BEFORE) ---
    has_usr = bool(getattr(ds, "SequenceOfUltrasoundRegions", None))
    row["has_sequence_of_ultrasound_regions"] = bool(has_usr)

    if not has_usr:
        row.update(
            {
                "status": "skipped_no_ultrasound_regions",
                "header_success": False,
                "pixel_success": False,
                "overwritten": False,
                "region_spatial_formats": "",
                "region_spatial_formats_allowed": False,
            }
        )
        append_row_to_worker_csv(row, out_csv, final_columns)
        return row

    rsf_vals = extract_region_spatial_formats(ds)
    row["region_spatial_formats"] = formats_to_str(rsf_vals)
    rsf_ok = any(v in allowed_rsf for v in rsf_vals)
    row["region_spatial_formats_allowed"] = bool(rsf_ok)

    if not rsf_ok:
        row.update(
            {
                "status": "skipped_disallowed_region_spatial_format",
                "header_success": False,
                "pixel_success": False,
                "overwritten": False,
            }
        )
        append_row_to_worker_csv(row, out_csv, final_columns)
        return row

    # --- HEADER PARSE ---
    try:
        parser.parse(remove_private=True)
        ds_after = parser.dicom

        # AFTER header values (existing traits only)
        for tag_name, col_name in traits:
            row[col_name] = safe_getattr(ds_after, tag_name, "")

        hashed_patient_id = sanitize_for_path(row.get("PatientID", ""), "NO_PATIENTID")
        hashed_study_uid = sanitize_for_path(
            row.get("StudyInstanceUID", ""), "NO_STUDYUID"
        )

        series_num_int = as_int_or_none(row.get("SeriesNumber", ""))
        inst_num_int = as_int_or_none(row.get("InstanceNumber", ""))

        series_uid = sanitize_for_path(row.get("SeriesInstanceUID", ""), "NO_SERIESUID")
        sop_instance_uid = sanitize_for_path(
            row.get("SOPInstanceUID", ""), "NO_SOPINSTANCEUID"
        )

        # collision protection (only when missing)
        suffix = uuid.uuid4().hex[:8]
        if hashed_patient_id == "NO_PATIENTID":
            hashed_patient_id = f"{hashed_patient_id}_{suffix}"
        if hashed_study_uid == "NO_STUDYUID":
            hashed_study_uid = f"{hashed_study_uid}_{suffix}"
        if series_uid == "NO_SERIESUID":
            series_uid = f"{series_uid}_{suffix}"
        if sop_instance_uid == "NO_SOPINSTANCEUID":
            sop_instance_uid = f"{sop_instance_uid}_{suffix}"

        series_part = fmt3(series_num_int) if series_num_int is not None else series_uid
        instance_part = (
            fmt5(inst_num_int) if inst_num_int is not None else sop_instance_uid
        )

        out_dir = OUTPUT_ROOT_ / hashed_patient_id / hashed_study_uid / series_part
        out_dir.mkdir(parents=True, exist_ok=True)

        base_name = f"{instance_part}.dcm"
        header_cleaned_path = out_dir / base_name

        parser.save(filename=str(header_cleaned_path), overwrite=True)

        row.update(
            {
                "header_path": str(header_cleaned_path),
                "header_success": True,
                "size_after_header": header_cleaned_path.stat().st_size,
                "patient_dir": hashed_patient_id,
                "study_dir": hashed_study_uid,
                "series_dir": series_part,
                "instance_filename": base_name,
            }
        )

    except Exception as e:
        row.update(
            {
                "status": "header_fail",
                "error": repr(e),
                "header_success": False,
                "pixel_success": False,
                "overwritten": False,
            }
        )
        append_row_to_worker_csv(row, out_csv, final_columns)
        return row

    # --- DEAL WITH ZONARE PROBLEM: USE DCMTK DECODE/REWRITE (AFTER HEADER, BEFORE PIXELS) ---
    try:
        manu = str(row.get("Manufacturer", "") or "").strip().upper()
        tsuid = str(row.get("TransferSyntaxUID_before", "") or "").strip()
        pc_raw = row.get("PlanarConfiguration_before", "")

        pc_val = None
        try:
            pc_val = float(pc_raw) if pc_raw != "" else None
        except Exception:
            pc_val = None

        needs_dcmtk = (
            manu == "ZONARE" and tsuid == "1.2.840.10008.1.2.4.70" and (pc_val == 1.0)
        )

        if needs_dcmtk:
            row["dcmtk_rewrite_attempted"] = True
            ok, err = dcmtk_decode_overwrite_inplace(header_cleaned_path)
            row["dcmtk_rewrite_success"] = bool(ok)

            if not ok:
                try:
                    if header_cleaned_path.exists():
                        header_cleaned_path.unlink()
                        row["header_path"] = ""
                        row["size_after_header"] = ""
                except Exception as cleanup_err:
                    row["error"] = (
                        row.get("error") or ""
                    ) + f" | header_cleanup_error={cleanup_err!r}"

                row.update(
                    {
                        "status": "dcmtk_fail",
                        "error": (row.get("error") or "") + f" | {err}",
                        "pixel_success": False,
                        "overwritten": False,
                        "pixel_path": "",
                        "size_after_pixel": "",
                    }
                )
                append_row_to_worker_csv(row, out_csv, final_columns)
                return row

    except Exception as e:
        try:
            if "header_cleaned_path" in locals() and header_cleaned_path.exists():
                header_cleaned_path.unlink()
                row["header_path"] = ""
                row["size_after_header"] = ""
        except Exception as cleanup_err:
            row["error"] = (
                row.get("error") or ""
            ) + f" | header_cleanup_error={cleanup_err!r}"

        row.update(
            {
                "status": "dcmtk_fail",
                "error": (row.get("error") or "") + f" | dcmtk_logic_error={e!r}",
                "dcmtk_rewrite_success": False,
                "pixel_success": False,
                "overwritten": False,
                "pixel_path": "",
                "size_after_pixel": "",
            }
        )
        append_row_to_worker_csv(row, out_csv, final_columns)
        return row

    # Decide buffer_pct based on CLI override or Manufacturer + ManufacturerModelName.
    buffer_pct = resolve_buffer_pct(
        buffer_pct,
        row.get("Manufacturer", ""),
        row.get("ManufacturerModelName", ""),
    )

    # --- PIXEL PASS ---
    try:
        pixel_total_start = time.perf_counter()
        cleaner = DicomCleaner(output_folder=str(out_dir), deid=str(RECIPE_PATH_))
        detect_start = time.perf_counter()
        cleaner.detect(
            str(header_cleaned_path), mask_above_top=True, buffer_pct=buffer_pct
        )
        row["detect_s"] = _format_elapsed(detect_start)

        tsuid_before_pixel = read_transfer_syntax_uid(header_cleaned_path)
        if tsuid_before_pixel == JPEG_BASELINE_TSUID:
            row["compressed_redaction_attempted"] = True
            row["compressed_redaction_codec"] = "jpeg_baseline"
            compressed_start = time.perf_counter()
            try:
                fast_ok, fast_err, fast_codec = jpeg_baseline_redact_overwrite_inplace(
                    header_cleaned_path,
                    cleaner.results,
                    jpeg_baseline_backend,
                )
            except Exception as e:
                fast_ok = False
                fast_err = repr(e)
                fast_codec = ""
            row["compressed_redaction_s"] = _format_elapsed(compressed_start)
            row["compressed_redaction_plan_cache_size"] = len(
                _JPEG_REDACTION_PLAN_CACHE
            )
            if fast_codec:
                row["compressed_redaction_codec"] = fast_codec

            if fast_ok:
                size_after_pixel = header_cleaned_path.stat().st_size
                row["pixel_total_s"] = _format_elapsed(pixel_total_start)
                row.update(
                    {
                        "pixel_path": str(header_cleaned_path),
                        "pixel_success": True,
                        "size_after_pixel": size_after_pixel,
                        "TransferSyntaxUID_after_pixel": read_transfer_syntax_uid(
                            header_cleaned_path
                        ),
                        "compressed_redaction_success": True,
                        "compressed_redaction_error": fast_err or "",
                        "pixel_redaction_method": fast_codec
                        or "compressed_jpeg_baseline",
                        "overwritten": True,
                        "status": "success",
                    }
                )
                append_row_to_worker_csv(row, out_csv, final_columns)
                return row

            row["compressed_redaction_error"] = fast_err or "unknown_error"
            if jpeg_baseline_backend == JPEG_BASELINE_BACKEND_REQUIRE_PIXELMED:
                row["pixel_total_s"] = _format_elapsed(pixel_total_start)
                row.update(
                    {
                        "pixel_redaction_method": "require_pixelmed",
                        "status": "pixel_fail",
                        "error": fast_err or "unknown_error",
                        "pixel_success": False,
                        "overwritten": False,
                        "pixel_path": "",
                        "size_after_pixel": "",
                    }
                )
                try:
                    if header_cleaned_path.exists():
                        header_cleaned_path.unlink()
                        row["header_path"] = ""
                        row["size_after_header"] = ""
                except Exception as cleanup_err:
                    row["error"] = (
                        row.get("error") or ""
                    ) + f" | header_cleanup_error={cleanup_err!r}"
                append_row_to_worker_csv(row, out_csv, final_columns)
                return row

            row["pixel_redaction_method"] = "decompressed_fallback"

        clean_start = time.perf_counter()
        cleaner.clean()
        row["clean_s"] = _format_elapsed(clean_start)

        save_start = time.perf_counter()
        pixel_cleaned = cleaner.save_dicom(filename=base_name, jpeg_ls=True)
        row["save_s"] = _format_elapsed(save_start)
        pixel_path = Path(pixel_cleaned)

        pixel_success = bool(pixel_path.is_file())
        size_after_pixel = pixel_path.stat().st_size if pixel_success else ""
        tsuid_after_pixel = (
            read_transfer_syntax_uid(pixel_path) if pixel_success else ""
        )
        row["pixel_total_s"] = _format_elapsed(pixel_total_start)

        overwritten = (
            pixel_success and pixel_path.resolve() == header_cleaned_path.resolve()
        )

        if not pixel_success or not overwritten:
            try:
                if pixel_path.exists():
                    pixel_path.unlink()
            except Exception:
                pass

            try:
                if header_cleaned_path.exists():
                    header_cleaned_path.unlink()
                    row["header_path"] = ""
                    row["size_after_header"] = ""
            except Exception as cleanup_err:
                row["error"] = (
                    row.get("error") or ""
                ) + f" | header_cleanup_error={cleanup_err!r}"

            row.update(
                {
                    "status": "pixel_fail",
                    "pixel_success": False,
                    "overwritten": False,
                    "pixel_path": "",
                    "size_after_pixel": "",
                }
            )
            append_row_to_worker_csv(row, out_csv, final_columns)
            return row

        row.update(
            {
                "pixel_path": str(pixel_path),
                "pixel_success": True,
                "size_after_pixel": size_after_pixel,
                "TransferSyntaxUID_after_pixel": tsuid_after_pixel,
                "pixel_redaction_method": row.get("pixel_redaction_method")
                or "decompressed_pixel_array",
                "overwritten": True,
                "status": "success",
            }
        )
        append_row_to_worker_csv(row, out_csv, final_columns)
        return row

    except Exception as e:
        if "pixel_total_start" in locals():
            row["pixel_total_s"] = _format_elapsed(pixel_total_start)
        try:
            if "pixel_cleaned" in locals() and pixel_cleaned:
                p = Path(pixel_cleaned)
                if p.exists():
                    p.unlink()
        except Exception:
            pass

        try:
            if header_cleaned_path.exists():
                header_cleaned_path.unlink()
                row["header_path"] = ""
                row["size_after_header"] = ""
        except Exception as cleanup_err:
            row["error"] = (
                row.get("error") or ""
            ) + f" | header_cleanup_error={cleanup_err!r}"

        row.update(
            {
                "status": "pixel_fail",
                "error": repr(e),
                "pixel_success": False,
                "overwritten": False,
                "pixel_path": "",
                "size_after_pixel": "",
            }
        )
        append_row_to_worker_csv(row, out_csv, final_columns)
        return row


def _process_one_star(args_tuple):
    """
    Helper for multiprocessing.Pool: unpack args and call process_one.
    """
    return process_one(*args_tuple)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="De-identify DICOM headers + clean pixels in parallel."
    )
    ap.add_argument(
        "--input-root",
        required=True,
        type=Path,
        help="Root folder to recursively scan for DICOM files.",
    )
    ap.add_argument(
        "--output-root",
        required=True,
        type=Path,
        help="Output root for de-identified DICOMs + logs.",
    )
    ap.add_argument(
        "--recipe-path", required=True, type=Path, help="Path to deid recipe"
    )
    ap.add_argument(
        "--salt",
        default=None,
        help="SECRET_SALT value. Overrides environment variable SECRET_SALT if provided.",
    )
    ap.add_argument(
        "--subsample",
        type=int,
        default=None,
        help="Optional subsample count for testing (random sample).",
    )
    ap.add_argument(
        "--workers", type=int, default=10, help="Number of parallel worker processes."
    )
    ap.add_argument(
        "--flush-every",
        type=int,
        default=500,
        help="Rebuild master log every N completed files.",
    )
    ap.add_argument(
        "--chunksize",
        type=int,
        default=CHUNKSIZE,
        help="Multiprocessing chunk size. Higher values improve JPEG plan-cache reuse.",
    )
    ap.add_argument(
        "--jpeg-baseline-backend",
        choices=JPEG_BASELINE_BACKEND_CHOICES,
        default=JPEG_BASELINE_BACKEND_AUTO,
        help=(
            "JPEG Baseline redaction backend policy. "
            "'auto' uses PixelMed when available and falls back to Python, "
            "'require-pixelmed' fails instead of falling back, and "
            "'python-only' skips PixelMed entirely."
        ),
    )
    ap.add_argument(
        "--buffer-pct",
        type=float,
        default=None,
        help="Optional global buffer override from 0.0 to 1.0. If provided, this overrides all manufacturer/model buffer settings.",
    )
    return ap.parse_args(argv)


def main() -> None:
    args = parse_args()
    args.buffer_pct = validate_buffer_pct(args.buffer_pct)
    args.jpeg_baseline_backend = validate_jpeg_baseline_backend(
        args.jpeg_baseline_backend
    )

    # ---------------- ENV ----------------
    if args.salt:
        os.environ["SECRET_SALT"] = args.salt
    elif os.getenv("SECRET_SALT"):
        pass
    else:
        raise RuntimeError(
            "SECRET_SALT is not set. Provide via --salt or environment variable SECRET_SALT."
        )

    print("SECRET_SALT set (source):", "CLI" if args.salt else "ENV")

    INPUT_ROOT = args.input_root
    OUTPUT_ROOT = args.output_root
    RECIPE_PATH = args.recipe_path

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_DIR = OUTPUT_ROOT / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MASTER_LOG_CSV = OUTPUT_ROOT / "deid_log.csv"
    jpeg_backend_status = assess_jpeg_baseline_backend(args.jpeg_baseline_backend)
    strict_preflight_error = None

    if args.jpeg_baseline_backend == JPEG_BASELINE_BACKEND_REQUIRE_PIXELMED:
        if not jpeg_backend_status["available"]:
            strict_preflight_error = jpeg_backend_status["message"]
            print("[ERROR]", jpeg_backend_status["message"])
        else:
            print("[INFO]", jpeg_backend_status["message"])
    elif args.jpeg_baseline_backend == JPEG_BASELINE_BACKEND_AUTO:
        if jpeg_backend_status["available"]:
            print("[INFO]", jpeg_backend_status["message"])
        else:
            print("[WARN]", jpeg_backend_status["message"])
    else:
        print("[INFO]", jpeg_backend_status["message"])

    # --- METRICS FILE (ADDED) ---
    METRICS_TXT = OUTPUT_ROOT / "metrics.txt"
    # (Header is written later, once ctx/worker/chunksize are known.)

    if not INPUT_ROOT.is_dir():
        raise NotADirectoryError(
            f"Input root does not exist or is not a directory: {INPUT_ROOT}"
        )

    in_root = INPUT_ROOT.resolve()
    out_root = OUTPUT_ROOT.resolve()
    if str(out_root).startswith(str(in_root) + os.sep):
        raise RuntimeError(
            f"OUTPUT_ROOT should not be inside INPUT_ROOT. "
            f"INPUT_ROOT={in_root} OUTPUT_ROOT={out_root}"
        )

    # ---------------- DISCOVER FILES ----------------
    dcm_files, total_files, done_src, todo = prepare_todo_files(
        INPUT_ROOT, LOG_DIR, args.subsample
    )
    if total_files == 0:
        print(f"[NOTE] No files found under {INPUT_ROOT}")
        raise SystemExit(0)

    if args.subsample is not None and args.subsample > 0:
        n = len(dcm_files)
        print(f"[INFO] Found {total_files} files; sampling {n}.")
    else:
        print(f"[INFO] Processing all {total_files} files.")

    # ---------------- RESUME SUPPORT ----------------
    print(f"[INFO] Resume: {len(done_src)} already done; {len(todo)} to process.")

    # ---------------- RUN PARALLEL  ----------------
    start_method = "spawn" if platform.system().lower().startswith("win") else "fork"
    ctx = mp.get_context(start_method)
    tasks = []
    n_workers = int(args.workers)
    chunksize = max(1, int(args.chunksize))

    # --- WRITE METRICS HEADER (RUN CONFIG) ---
    with open(METRICS_TXT, "w", encoding="utf-8") as f:
        f.write("# deid-echo metrics\n")
        f.write(f"# start_time: {_metrics_ts()}\n")
        f.write(f"# workers: {n_workers}\n")
        f.write(f"# chunksize: {chunksize}\n")
        f.write(f"# jpeg_baseline_backend: {args.jpeg_baseline_backend}\n")
        f.write(f"# pixelmed_status: {jpeg_backend_status['status']}\n")
        f.write(f"# pixelmed_message: {jpeg_backend_status['message']}\n")
        for key in ("java_path", "javac_path", "jar_path", "class_dir", "error"):
            value = jpeg_backend_status["diagnostics"].get(key, "")
            if value:
                f.write(f"# pixelmed_{key}: {value}\n")
        f.write(f"# multiprocessing_method: {ctx.get_start_method()}\n")
        f.write(f"# maxtasksperchild: {MAX_TASKS_PER_CHILD}\n")
        f.write("# -----------------------------------------\n")
        f.write("timestamp\tcompleted/total\telapsed_s\tavg_fps\tinterval_fps\n")

    # ---- GLOBAL + INTERVAL SPEED TIMING ----
    run_start_ts = time.time()
    interval_start_ts = run_start_ts
    interval_completed = 0
    completed = 0
    total_todo = len(todo)

    # Optional: initial baseline line (0 progress)
    append_metrics_line(
        METRICS_TXT,
        completed=0,
        total=total_todo,
        run_start_ts=run_start_ts,
        interval_completed=0,
        interval_start_ts=interval_start_ts,
    )

    if strict_preflight_error:
        with open(METRICS_TXT, "a", encoding="utf-8") as f:
            f.write("# -----------------------------------------\n")
            f.write(f"# end_time: {_metrics_ts()}\n")
            f.write("# completed: 0/0\n")
            f.write(f"# startup_error: {strict_preflight_error}\n")
        raise RuntimeError(strict_preflight_error)

    if len(todo) == 0:
        print("[INFO] Nothing to do. Rebuilding master log and exiting.")
        rebuild_master_log(MASTER_LOG_CSV, LOG_DIR, FINAL_COLUMNS)
        print(f"[INFO] Master log written: {MASTER_LOG_CSV}")

        end_ts = time.time()
        total_elapsed = end_ts - run_start_ts

        # final snapshot line
        append_metrics_line(
            METRICS_TXT,
            completed=0,
            total=0,
            run_start_ts=run_start_ts,
            interval_completed=0,
            interval_start_ts=interval_start_ts,
        )

        # --- METRICS FOOTER (END OF RUN) ---
        with open(METRICS_TXT, "a", encoding="utf-8") as f:
            f.write("# -----------------------------------------\n")
            f.write(f"# end_time: {_metrics_ts()}\n")
            f.write(f"# total_elapsed_s: {total_elapsed:.1f}\n")
            f.write("# completed: 0/0\n")
            f.write("# avg_fps: 0.000\n")

        raise SystemExit(0)

    for idx, path_str in enumerate(todo):
        worker_id = idx % n_workers
        tasks.append(
            (
                path_str,
                worker_id,
                str(INPUT_ROOT),
                str(OUTPUT_ROOT),
                str(RECIPE_PATH),
                str(LOG_DIR),
                args.jpeg_baseline_backend,
                args.buffer_pct,
                ALLOWED_SOP,
                ALLOWED_RSF,
                TRAITS,
                FINAL_COLUMNS,
            )
        )

    try:
        with ctx.Pool(
            processes=n_workers,
            initializer=_init_worker,
            maxtasksperchild=MAX_TASKS_PER_CHILD,
        ) as pool:
            for _ in pool.imap_unordered(
                _process_one_star, tasks, chunksize=chunksize
            ):
                completed += 1
                interval_completed += 1

                if completed % int(args.flush_every) == 0:
                    now_ts = time.time()
                    elapsed_total = now_ts - run_start_ts
                    avg_fps = (completed / elapsed_total) if elapsed_total > 0 else 0.0

                    elapsed_interval = now_ts - interval_start_ts
                    interval_fps = (
                        interval_completed / elapsed_interval
                        if elapsed_interval > 0
                        else 0.0
                    )

                    rebuild_master_log(MASTER_LOG_CSV, LOG_DIR, FINAL_COLUMNS)

                    append_metrics_line(
                        METRICS_TXT,
                        completed=completed,
                        total=total_todo,
                        run_start_ts=run_start_ts,
                        interval_completed=interval_completed,
                        interval_start_ts=interval_start_ts,
                    )

                    print(
                        f"[INFO] Progress: {completed}/{total_todo} "
                        f"| avg={avg_fps:.2f} files/sec | interval={interval_fps:.2f} files/sec"
                    )

                    interval_start_ts = now_ts
                    interval_completed = 0

    finally:
        end_ts = time.time()
        total_elapsed = end_ts - run_start_ts
        final_avg_fps = (completed / total_elapsed) if total_elapsed > 0 else 0.0

        last_interval_elapsed = end_ts - interval_start_ts
        last_interval_fps = (
            interval_completed / last_interval_elapsed
            if last_interval_elapsed > 0
            else 0.0
        )

        try:
            rebuild_master_log(MASTER_LOG_CSV, LOG_DIR, FINAL_COLUMNS)
        except Exception:
            pass

        append_metrics_line(
            METRICS_TXT,
            completed=completed,
            total=total_todo,
            run_start_ts=run_start_ts,
            interval_completed=interval_completed,
            interval_start_ts=interval_start_ts,
        )

        # --- METRICS FOOTER (END OF RUN) ---
        with open(METRICS_TXT, "a", encoding="utf-8") as f:
            f.write("# -----------------------------------------\n")
            f.write(f"# end_time: {_metrics_ts()}\n")
            f.write(f"# total_elapsed_s: {total_elapsed:.1f}\n")
            f.write(f"# completed: {completed}/{total_todo}\n")
            f.write(f"# avg_fps: {final_avg_fps:.3f}\n")

        print(
            f"[INFO] Done: {completed}/{total_todo} "
            f"| total_time={total_elapsed:.1f}s "
            f"| avg speed={final_avg_fps:.2f} files/sec "
            f"| last interval={last_interval_fps:.2f} files/sec"
        )

    print(f"[INFO] Master log written: {MASTER_LOG_CSV}")
    print(f"[INFO] Worker logs are in: {LOG_DIR}")
    print(f"[INFO] Metrics written: {METRICS_TXT}")


if __name__ == "__main__":
    main()
