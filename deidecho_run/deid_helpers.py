import csv
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


# -----------------------------
# Basic helpers
# -----------------------------
def safe_getattr(ds, name, default="") -> str:
    try:
        val = getattr(ds, name, default)
        return default if val is None else str(val)
    except Exception:
        return default


def sanitize_for_path(s: str, default: str = "UNKNOWN") -> str:
    if s is None:
        return default
    s = str(s).strip()
    if not s:
        return default
    # keep this conservative: avoid path separators
    return s.replace("/", "_").replace("\\", "_").replace(os.sep, "_")


# -----------------------------
# Ultrasound region helpers
# -----------------------------
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


# -----------------------------
# Worker CSV append
# -----------------------------
def append_row_to_worker_csv(
    row: Dict[str, Any],
    out_csv: Path,
    final_columns: List[str],
) -> None:
    """
    Single-process append. Each worker writes to its own CSV (no contention).
    Always writes the full final_columns schema in order (prevents header drift).
    """
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    file_exists = out_csv.exists()

    out_row = {col: row.get(col, "") for col in final_columns}

    with open(out_csv, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=final_columns)
        if not file_exists:
            writer.writeheader()
        writer.writerow(out_row)


# -----------------------------
# Formatting helpers
# -----------------------------
def as_int_or_none(s: Any) -> Optional[int]:
    """Best-effort parse a DICOM-ish string as int. Returns None if missing/unparseable."""
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


# -----------------------------
# Resume support (NO pandas)
# -----------------------------
def load_done_src_paths_from_worker_logs(log_dir: Path) -> Set[str]:
    """Resume support: stream worker logs and return src_paths already processed."""
    done: Set[str] = set()
    for p in sorted(log_dir.glob("deid_header_pixel_log__worker_*.csv")):
        try:
            with open(p, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames or "src_path" not in reader.fieldnames:
                    continue
                for row in reader:
                    src = (row.get("src_path") or "").strip()
                    if src:
                        done.add(src)
        except Exception:
            continue
    return done


# -----------------------------
# Append-only master log (incremental)
# -----------------------------
def _src_index_path_for(master_csv: Path, log_dir: Path) -> Path:
    return log_dir / f"{master_csv.stem}_src_index.txt"


def _state_path_for(master_csv: Path, log_dir: Path) -> Path:
    return log_dir / f"{master_csv.stem}_append_state.json"


def _load_src_index(index_path: Path) -> Set[str]:
    seen: Set[str] = set()
    if index_path.exists() and index_path.stat().st_size > 0:
        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s:
                    seen.add(s)
    return seen


def _append_src_index(index_path: Path, srcs: List[str]) -> None:
    if not srcs:
        return
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path, "a", encoding="utf-8") as f:
        for s in srcs:
            f.write(s.replace("\n", " ").strip() + "\n")


def _load_state(state_path: Path) -> Dict[str, int]:
    if state_path.exists() and state_path.stat().st_size > 0:
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): int(v) for k, v in data.items()}
        except Exception:
            return {}
    return {}


def _save_state(state_path: Path, state: Dict[str, int]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    tmp.replace(state_path)


def rebuild_master_log(
    master_csv: Path, log_dir: Path, final_columns: List[str]
) -> None:
    """
    Append-only, incremental, resume-safe master log builder.

    - Master CSV is append-only (never rewritten)
    - Writes header if master missing OR empty (handles crash-created empty file)
    - Never duplicates src_path across flushes or reruns
    - Efficient for repeated calls: reads only new content from each worker log using
      per-worker byte offsets stored in a state file.

    Sidecars (stored in log_dir so you can see them):
      - log_dir/<master_stem>_src_index.txt
      - log_dir/<master_stem>_append_state.json
    """
    master_csv.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    index_path = _src_index_path_for(master_csv, log_dir)
    state_path = _state_path_for(master_csv, log_dir)

    master_exists = master_csv.exists()
    master_empty = (not master_exists) or (master_csv.stat().st_size == 0)
    write_header = master_empty

    # Load seen srcs from index; if absent but master non-empty, seed it once from master.
    seen = _load_src_index(index_path)
    if not seen and master_exists and not master_empty:
        try:
            with open(master_csv, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames and "src_path" in reader.fieldnames:
                    for row in reader:
                        src = (row.get("src_path") or "").strip()
                        if src:
                            seen.add(src)
        except Exception as e:
            raise RuntimeError(
                f"Failed to read existing master log {master_csv}: {e!r}"
            )

        # Persist seed so next flush doesn't rescan master
        _append_src_index(index_path, sorted(seen))

    state = _load_state(state_path)
    appended_srcs: List[str] = []

    with open(master_csv, "a", encoding="utf-8", newline="") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=final_columns)
        if write_header:
            writer.writeheader()

        for worker_csv in sorted(log_dir.glob("deid_header_pixel_log__worker_*.csv")):
            key = worker_csv.name
            offset = int(state.get(key, 0))

            try:
                size = worker_csv.stat().st_size
            except Exception:
                continue

            if offset > size:
                offset = 0  # worker log truncated/rotated

            try:
                with open(worker_csv, "r", encoding="utf-8", newline="") as in_f:
                    header_line = in_f.readline()
                    if not header_line:
                        continue

                    fieldnames = [h.strip() for h in header_line.strip().split(",")]
                    if "src_path" not in fieldnames:
                        continue

                    if offset <= 0:
                        # positioned after header already
                        pass
                    else:
                        in_f.seek(offset)
                        if in_f.tell() == 0:
                            _ = in_f.readline()  # skip header again

                    reader = csv.DictReader(in_f, fieldnames=fieldnames)

                    for row in reader:
                        src = (row.get("src_path") or "").strip()
                        if not src or src in seen:
                            continue

                        out_row = {
                            col: (row.get(col, "") or "") for col in final_columns
                        }
                        writer.writerow(out_row)

                        seen.add(src)
                        appended_srcs.append(src)

                    state[key] = in_f.tell()

            except Exception:
                continue

    if appended_srcs:
        _append_src_index(index_path, appended_srcs)
    _save_state(state_path, state)
