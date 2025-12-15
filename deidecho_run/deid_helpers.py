import csv
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


def safe_getattr(ds, name, default="") -> str:
    try:
        val = getattr(ds, name, default)
        return default if val is None else str(val)
    except Exception:
        return default


def sanitize_for_path(s: str, default: str = "UNKNOWN") -> str:
    if not s:
        return default
    s = str(s).strip()
    if not s:
        return default
    return s.replace("/", "_").replace("\\", "_").replace(os.sep, "_")


def extract_region_spatial_formats(ds) -> List[int]:
    """Return sorted unique RegionSpatialFormat values found in SequenceOfUltrasoundRegions."""
    try:
        seq = getattr(ds, "SequenceOfUltrasoundRegions", None)
        if not seq:
            return []
        vals: List[int] = []
        for item in seq:
            v = getattr(item, "RegionSpatialFormat", None)
            if v is None:
                continue
            try:
                vals.append(int(v))
            except Exception:
                continue
        return sorted(set(vals))
    except Exception:
        return []


def formats_to_str(formats: List[int]) -> str:
    return ",".join(str(x) for x in sorted(set(formats))) if formats else ""


def append_row_to_worker_csv(
    row: Dict[str, Any], out_csv: Path, final_columns: List[str]
) -> None:
    """
    Single-process append. Each worker writes to its own CSV (no contention).
    Always writes the full final_columns schema in order (prevents header drift).
    """
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    file_exists = out_csv.exists()

    out_row = {col: row.get(col, "") for col in final_columns}

    with open(out_csv, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=final_columns)
        if not file_exists:
            writer.writeheader()
        writer.writerow(out_row)


def load_done_src_paths_from_worker_logs(log_dir: Path) -> set:
    """Resume support: read worker logs and return src_paths already processed."""
    done = set()
    for p in sorted(log_dir.glob("deid_header_pixel_log__worker_*.csv")):
        try:
            df = pd.read_csv(p, usecols=["src_path"])
            done.update(df["src_path"].astype(str).tolist())
        except Exception:
            continue
    return done


def rebuild_master_log(
    master_csv: Path, log_dir: Path, final_columns: List[str]
) -> None:
    """
    Simplified master rebuild:
      - concat all worker CSVs (they share final_columns)
      - dedup by src_path (keep last)
      - sort by src_path
      - write master_csv
    """
    parts = []
    for p in sorted(log_dir.glob("deid_header_pixel_log__worker_*.csv")):
        try:
            parts.append(pd.read_csv(p, dtype=str))
        except Exception:
            continue
    if not parts:
        return

    df = pd.concat(parts, ignore_index=True)

    if "src_path" not in df.columns:
        return
    df["src_path"] = df["src_path"].astype(str)
    df = df.drop_duplicates(subset=["src_path"], keep="last")

    # Keep sort stable/deterministic
    df = df.sort_values(by=["src_path"], na_position="last", kind="mergesort")

    # Ensure exact column order in output
    for col in final_columns:
        if col not in df.columns:
            df[col] = ""
    df = df[final_columns]

    master_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(master_csv, index=False)


def as_int_or_none(s: str) -> Optional[int]:
    """Best-effort parse a DICOM IS/DS-ish string as int. Returns None if missing/unparseable."""
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def fmt3(n: int) -> str:
    return f"{n:03d}"


def fmt5(n: int) -> str:
    return f"{n:05d}"
