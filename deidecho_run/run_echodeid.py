import argparse
import os
import random
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Set

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

from deid.dicom.parser import DicomParser
from deid.dicom.pixels.clean import DicomCleaner

# ---------------- CONSTANTS ----------------
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
    ("Modality", "Modality"),
]

FINAL_COLUMNS = [
    "input_root",
    "src_path",
    "SECRET_SALT_before",
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
    # --- AFTER values  ---
    "PatientID",
    "StudyDate",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "SOPClassUID",
    "InstanceNumber",
    "SeriesNumber",
    "Modality",
    "header_path",
    "header_success",
    "size_after_header",
    "patient_dir",
    "series_dir",
    "instance_filename",
    "size_before_pixel",
    "pixel_path",
    "pixel_success",
    "size_after_pixel",
    "overwritten",
    "status",
    "error",
]


def worker_log_path(log_dir: Path, worker_id: int) -> Path:
    return log_dir / f"deid_header_pixel_log__worker_{worker_id:03d}.csv"


def process_one(
    dcm_path_str: str,
    worker_id: int,
    input_root: str,
    output_root: str,
    recipe_path: str,
    log_dir: str,
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
    }

    out_csv = worker_log_path(LOG_DIR_, worker_id)

    # --- READ ORIGINAL ---
    try:
        parser = DicomParser(str(dcm_path), recipe=str(RECIPE_PATH_))
        ds = parser.dicom
    except Exception as e:
        row.update({"status": "read_error", "error": repr(e)})
        append_row_to_worker_csv(row, out_csv, final_columns)
        return row

    # --- BEFORE HEADER (requested BEFORE-only values) ---
    row["SECRET_SALT_before"] = os.getenv("SECRET_SALT", "")

    # TransferSyntaxUID lives on file_meta in pydicom
    ts_uid = ""
    try:
        if getattr(ds, "file_meta", None) is not None:
            ts_uid = str(getattr(ds.file_meta, "TransferSyntaxUID", "") or "")
    except Exception:
        ts_uid = ""
    row["TransferSyntaxUID_before"] = ts_uid

    row["PhotometricInterpretation_before"] = safe_getattr(
        ds, "PhotometricInterpretation", ""
    )
    row["PlanarConfiguration_before"] = safe_getattr(ds, "PlanarConfiguration", "")

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
        if series_uid == "NO_SERIESUID":
            series_uid = f"{series_uid}_{suffix}"
        if sop_instance_uid == "NO_SOPINSTANCEUID":
            sop_instance_uid = f"{sop_instance_uid}_{suffix}"

        series_part = fmt3(series_num_int) if series_num_int is not None else series_uid
        instance_part = (
            fmt5(inst_num_int) if inst_num_int is not None else sop_instance_uid
        )

        out_dir = OUTPUT_ROOT_ / hashed_patient_id / series_part
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

    # --- PIXEL PASS ---
    try:
        size_before_pixel = header_cleaned_path.stat().st_size
        row["size_before_pixel"] = size_before_pixel

        cleaner = DicomCleaner(output_folder=str(out_dir), deid=str(RECIPE_PATH_))
        cleaner.detect(str(header_cleaned_path), mask_above_top=True, buffer_pct=0.01)
        cleaner.clean()

        pixel_cleaned = cleaner.save_dicom(filename=base_name, jpeg_ls=True)
        pixel_path = Path(pixel_cleaned)

        size_after_pixel = pixel_path.stat().st_size if pixel_path.is_file() else ""

        overwritten = (
            pixel_path.is_file()
            and pixel_path.resolve() == header_cleaned_path.resolve()
            and str(size_after_pixel) != str(size_before_pixel)
        )

        row.update(
            {
                "pixel_path": str(pixel_path),
                "pixel_success": bool(pixel_path.is_file()),
                "size_after_pixel": size_after_pixel,
                "overwritten": bool(overwritten),
                "status": "success" if overwritten else "pixel_written_but_size_same",
            }
        )
    except Exception as e:
        row.update(
            {
                "status": "pixel_fail",
                "error": repr(e),
                "pixel_success": False,
                "overwritten": False,
            }
        )

    append_row_to_worker_csv(row, out_csv, final_columns)
    return row


def parse_args() -> argparse.Namespace:
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
        "--workers", type=int, default=100, help="Number of parallel worker processes."
    )
    ap.add_argument(
        "--flush-every",
        type=int,
        default=20,
        help="Rebuild master log every N completed files.",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    # ---------------- ENV ----------------
    if args.salt:
        # CLI takes precedence
        os.environ["SECRET_SALT"] = args.salt
    elif os.getenv("SECRET_SALT"):
        # Use existing environment variable
        pass
    else:
        raise RuntimeError(
            "SECRET_SALT is not set. Provide via --salt or environment variable SECRET_SALT."
        )

    print("SECRET_SALT set (source):", "CLI" if args.salt else "ENV")

    # ---------------- CONFIG ----------------
    INPUT_ROOT = args.input_root
    OUTPUT_ROOT = args.output_root
    RECIPE_PATH = args.recipe_path

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    LOG_DIR = OUTPUT_ROOT / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    MASTER_LOG_CSV = OUTPUT_ROOT / "deid_log.csv"

    if not INPUT_ROOT.is_dir():
        raise NotADirectoryError(
            f"Input root does not exist or is not a directory: {INPUT_ROOT}"
        )

    # ---------------- DISCOVER FILES ----------------
    dcm_files = sorted(p for p in INPUT_ROOT.rglob("*") if p.is_file())
    total_files = len(dcm_files)
    if total_files == 0:
        print(f"[NOTE] No files found under {INPUT_ROOT}")
        raise SystemExit(0)

    # Optional subsample
    if args.subsample is not None and args.subsample > 0:
        n = min(args.subsample, total_files)
        dcm_files = random.sample(dcm_files, n)
        print(f"[INFO] Found {total_files} files; sampling {n}.")
    else:
        print(f"[INFO] Processing all {total_files} files.")

    # ---------------- RESUME SUPPORT ----------------
    done_src = load_done_src_paths_from_worker_logs(LOG_DIR)
    todo = [str(p) for p in dcm_files if str(p) not in done_src]
    print(f"[INFO] Resume: {len(done_src)} already done; {len(todo)} to process.")

    if len(todo) == 0:
        print("[INFO] Nothing to do. Rebuilding master log and exiting.")
        rebuild_master_log(MASTER_LOG_CSV, LOG_DIR, FINAL_COLUMNS)
        print(f"[INFO] Master log written: {MASTER_LOG_CSV}")
        raise SystemExit(0)

    # ---------------- RUN PARALLEL ----------------
    futures = []
    with ProcessPoolExecutor(max_workers=int(args.workers)) as ex:
        for idx, path_str in enumerate(todo):
            worker_id = idx % int(args.workers)
            futures.append(
                ex.submit(
                    process_one,
                    path_str,
                    worker_id,
                    str(INPUT_ROOT),
                    str(OUTPUT_ROOT),
                    str(RECIPE_PATH),
                    str(LOG_DIR),
                    ALLOWED_SOP,
                    ALLOWED_RSF,
                    TRAITS,
                    FINAL_COLUMNS,
                )
            )

        completed = 0
        for fut in as_completed(futures):
            _ = fut.result()
            completed += 1

            if completed % int(args.flush_every) == 0:
                rebuild_master_log(MASTER_LOG_CSV, LOG_DIR, FINAL_COLUMNS)
                print(
                    f"[INFO] Progress: {completed}/{len(todo)} done. Master log updated."
                )

    rebuild_master_log(MASTER_LOG_CSV, LOG_DIR, FINAL_COLUMNS)
    print(f"[INFO] Done. Master log written: {MASTER_LOG_CSV}")
    print(f"[INFO] Worker logs are in: {LOG_DIR}")


if __name__ == "__main__":
    main()
