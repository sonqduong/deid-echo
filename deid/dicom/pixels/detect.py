__author__ = "Vanessa Sochat, Son Duong"
__copyright__ = "Copyright 2016-2025"
__license__ = "MIT"

from typing import Iterable, List, Optional, Tuple, Union

from pydicom import FileDataset
from pydicom.sequence import Sequence

import deid.dicom.utils as utils
from deid.config import DeidRecipe
from deid.dicom.filter import apply_filter
from deid.logger import bot


def evaluate_group(flags):
    """Evaluate group will take a list of flags (e.g., [True, 'and', False, 'or', True])"""
    flagged = False
    first_entry = True

    # If it starts with and/or, remove it
    if flags and flags[0] in ["and", "or"]:
        flags.pop(0)

    while len(flags) > 0:
        flag = flags.pop(0)
        if flag == "and":
            flag = flags.pop(0)
            flagged = flag and flagged
        elif flag == "or":
            flag = flags.pop(0)
            flagged = flag or flagged
        else:
            flagged = flag if first_entry else (flagged and flag)
        first_entry = False
    return flagged


def has_burned_pixels(
    dicom_files,
    force: bool = True,
    deid: Optional[DeidRecipe] = None,
    allowed_rsf: Optional[Iterable[int]] = (1, 2, 3),
    allowed_rdt: Optional[Iterable[int]] = None,
    *,
    mask_above_top: bool = False,
    buffer_pct: float = 0.0,
):
    """
    Determine if a DICOM file has burned pixels.

    Parameters:
    - allowed_rsf: iterable of RegionSpatialFormat integers to keep when extracting coordinates
      from SequenceOfUltrasoundRegions. Defaults to (1,2,3).
    - allowed_rdt: optional iterable of RegionDataType integers to keep (None = ignore).
    - mask_above_top: if True, instead of returning individual region boxes, return a single
      rectangle that spans the full width from y = (min_y + buffer_px) to the bottom of the image
      (i.e., KEEP area below that line; the blacked-out region is 0..start_y-1).
    - buffer_pct: fraction of total image height to ADD to min_y (0..1). Larger values → larger
      blacked-out band at the top.
    """
    if not isinstance(deid, DeidRecipe):
        if deid is None:
            deid = "dicom"
        deid = DeidRecipe(deid)

    if isinstance(dicom_files, list):  # list of paths or FileDataset
        return _has_burned_pixels_multi(
            dicom_files,
            force,
            deid,
            allowed_rsf=allowed_rsf,
            allowed_rdt=allowed_rdt,
            mask_above_top=mask_above_top,
            buffer_pct=buffer_pct,
        )
    return _has_burned_pixels_single(
        dicom_files,
        force,
        deid,
        allowed_rsf=allowed_rsf,
        allowed_rdt=allowed_rdt,
        mask_above_top=mask_above_top,
        buffer_pct=buffer_pct,
    )


def _has_burned_pixels_multi(
    dicom_files: List[Union[str, FileDataset]],
    force,
    deid,
    *,
    allowed_rsf: Optional[Iterable[int]],
    allowed_rdt: Optional[Iterable[int]],
    mask_above_top: bool,
    buffer_pct: float,
):
    decision = {"clean": [], "flagged": {}}
    bot.debug(f"[detect] evaluating {len(dicom_files)} files")
    for dicom_file in dicom_files:
        result = _has_burned_pixels_single(
            dicom_file=dicom_file,
            force=force,
            deid=deid,
            allowed_rsf=allowed_rsf,
            allowed_rdt=allowed_rdt,
            mask_above_top=mask_above_top,
            buffer_pct=buffer_pct,
        )
        if result["flagged"] is False:
            decision["clean"].append(dicom_file)
        else:
            decision["flagged"][dicom_file] = result
    return decision


def _has_burned_pixels_single(
    dicom_file,
    force: bool,
    deid,
    *,
    allowed_rsf: Optional[Iterable[int]],
    allowed_rdt: Optional[Iterable[int]],
    mask_above_top: bool,
    buffer_pct: float,
):
    bot.debug(f"[detect] loading DICOM: {dicom_file}")
    dicom = utils.load_dicom(dicom_file, force=force)
    results = []
    global_flagged = False

    filters = deid.get_filters()
    if not filters:
        bot.warning("Deid provided does not have filters.")
        return {"flagged": global_flagged, "results": results}

    for name, items in filters.items():
        bot.debug(f"[detect] applying filter group: {name} (items={len(items)})")
        for item_idx, item in enumerate(items):
            flags = []
            descriptions = []
            last_group = {}  # prevent unbound access later

            # A) no header filters, only coordinates → treat as True match
            if not item.get("filters") and item.get("coordinates"):
                this_item_flags = [True]
                group_descriptions = [item.get("name", "")]
                bot.debug(
                    f"[detect] item#{item_idx}: no filters, coordinates present → match=True"
                )
            else:
                this_item_flags = []
                group_descriptions = []
                for group_idx, group in enumerate(item.get("filters", [])):
                    last_group = group
                    this_group_flags = []
                    for a in range(len(group["action"])):
                        action = group["action"][a]
                        field = group["field"][a]
                        value = group["value"][a] if len(group["value"]) > a else ""
                        flag = apply_filter(
                            dicom=dicom,
                            field=field,
                            filter_name=action,
                            value=value or None,
                        )
                        this_group_flags.append(flag)
                        desc = f"{field} {action} {value}"
                        if len(group.get("InnerOperators", [])) > a:
                            inner_op = group["InnerOperators"][a]
                            this_group_flags.append(inner_op)
                            desc = f"{desc} {inner_op}"
                        group_descriptions.append(desc)
                        bot.debug(f"[detect] group#{group_idx} eval: {desc} → {flag}")
                    overall_group_flag = evaluate_group(this_group_flags)
                    this_item_flags.append(overall_group_flag)
                    bot.debug(
                        f"[detect] group#{group_idx} overall → {overall_group_flag}"
                    )

            # Evaluate the item-level result
            item_flag = evaluate_group(this_item_flags)

            # ---- FIXED: only add a valid operator ('and'/'or') when joining with prior flags ----
            op = (
                last_group["operator"]
                if isinstance(last_group, dict) and "operator" in last_group
                else None
            )
            if op in ("and", "or") and len(flags) > 0:
                flags.append(op)
            # Whether or not an operator was present/valid, append the boolean result
            flags.append(item_flag)

            # Human-readable reason string (include op only if valid)
            if op in ("and", "or"):
                reason = (f"{op} " + " ".join(group_descriptions)).replace("\n", " ")
            else:
                reason = (" ".join(group_descriptions)).replace("\n", " ")
            descriptions.append(reason)

            flagged = evaluate_group(flags=flags)
            bot.debug(
                f"[detect] item#{item_idx} item_flag={item_flag} operator={op!r} → flagged={flagged}"
            )

            if flagged is True:
                global_flagged = True
                reason = " ".join(descriptions)

                # Resolve any "from:" coordinate sources with RSF/RDT filtering
                coords_before = sum(
                    len(cs[1]) if isinstance(cs[1], list) else 1
                    for cs in item.get("coordinates", [])
                )
                for c_idx, coordset in enumerate(item.get("coordinates", [])):
                    # coordset is expected like: [mask_value, coordinates_or_from_str]
                    if len(coordset) != 2:
                        bot.warning(
                            f"[detect] coordset#{c_idx} malformed: {coordset!r}"
                        )
                        continue
                    mask_value, coord_spec = coordset
                    if isinstance(coord_spec, str) and coord_spec.startswith("from:"):
                        new_coords = extract_coordinates(
                            dicom,
                            coord_spec,
                            allowed_rsf=allowed_rsf,
                            allowed_rdt=allowed_rdt,
                            mask_above_top=mask_above_top,
                            buffer_pct=buffer_pct,
                        )
                        bot.debug(
                            f"[detect] coordset#{c_idx} resolved from {coord_spec!r} "
                            f"→ {len(new_coords)} coords (rsf={allowed_rsf}, rdt={allowed_rdt}, "
                            f"mask_above_top={mask_above_top}, buffer_pct={buffer_pct})"
                        )
                        coordset[1] = new_coords

                coords_after = sum(
                    len(cs[1]) if isinstance(cs[1], list) else 1
                    for cs in item.get("coordinates", [])
                )
                bot.debug(
                    f"[detect] coordinates resolved: before={coords_before}, after={coords_after}"
                )

                results.append(
                    {
                        "reason": reason,
                        "group": name,
                        "coordinates": item.get("coordinates", []),
                    }
                )

    bot.debug(f"[detect] finished: flagged={global_flagged}, results={len(results)}")
    return {"flagged": global_flagged, "results": results}


def _img_size(dicom) -> Tuple[int, int]:
    """Return (width_x, height_y) from Columns/Rows, or (0,0) if missing."""
    try:
        width = int(getattr(dicom, "Columns"))
        height = int(getattr(dicom, "Rows"))
        return width, height
    except Exception:
        return 0, 0


def extract_coordinates(
    dicom,
    field: str,
    *,
    allowed_rsf: Optional[Iterable[int]] = (1, 2, 3),
    allowed_rdt: Optional[Iterable[int]] = None,
    mask_above_top: bool = False,
    buffer_pct: float = 0.0,
):
    """
    Given a field that is provided for a dicom, extract coordinates.

    If mask_above_top is False (default): return per-region rectangles in the format
    "xmin,ymin,xmax,ymax" (unchanged behavior).

    If mask_above_top is True:
      - Compute the smallest top edge (min_y) among all allowed regions.
      - Convert buffer_pct (0..1) to pixels using total image height.
      - Return a single rectangle spanning the full width from y = (min_y + buffer_px)
        to the bottom of the image — this is the area to KEEP. The blacked-out region
        will be 0..start_y-1 and therefore grows as buffer_pct increases.
    """
    field = field.replace("from:", "", 1)
    coordinates = []

    if field not in dicom:
        bot.debug(f"[detect] extract_coordinates: field {field!r} not in dicom")
        return coordinates

    # Normalize selections to sets (or None)
    rsf_set = set(allowed_rsf) if allowed_rsf is not None else None
    rdt_set = set(allowed_rdt) if allowed_rdt is not None else None

    region_elem = dicom.get(field)
    regions = list(region_elem) if isinstance(region_elem, Sequence) else [region_elem]

    width, height = _img_size(dicom)
    bot.debug(
        f"[detect] extract_coordinates: regions={len(regions)}, rsf_allow={rsf_set}, "
        f"rdt_allow={rdt_set}, size=({width}x{height}), mask_above_top={mask_above_top}, "
        f"buffer_pct={buffer_pct}"
    )

    # Collect allowed region rectangles
    region_boxes: List[Tuple[int, int, int, int]] = []
    for i, region in enumerate(regions):
        rsf = getattr(region, "RegionSpatialFormat", None)
        rdt = getattr(region, "RegionDataType", None)
        try:
            rsf_i = int(rsf) if rsf is not None else None
        except Exception:
            rsf_i = None
        try:
            rdt_i = int(rdt) if rdt is not None else None
        except Exception:
            rdt_i = None

        if rsf_set is not None and rsf_i not in rsf_set:
            bot.debug(f"[detect] region#{i} skip rsf={rsf_i}")
            continue
        if rdt_set is not None and rdt_i not in rdt_set:
            bot.debug(f"[detect] region#{i} skip rdt={rdt_i}")
            continue

        have_all = all(
            hasattr(region, attr)
            for attr in (
                "RegionLocationMinX0",
                "RegionLocationMinY0",
                "RegionLocationMaxX1",
                "RegionLocationMaxY1",
            )
        )
        if have_all:
            xmin = int(region.RegionLocationMinX0)
            ymin = int(region.RegionLocationMinY0)
            xmax = int(region.RegionLocationMaxX1)
            ymax = int(region.RegionLocationMaxY1)
            region_boxes.append((xmin, ymin, xmax, ymax))
            bot.debug(
                f"[detect] region#{i} add box [{xmin},{ymin},{xmax},{ymax}] (rsf={rsf_i}, rdt={rdt_i})"
            )
        else:
            bot.debug(f"[detect] region#{i} missing location tags; skipping")

    if not region_boxes:
        bot.debug("[detect] extract_coordinates: no allowed region boxes; returning []")
        return coordinates

    if not mask_above_top:
        coordinates = [f"{x0},{y0},{x1},{y1}" for (x0, y0, x1, y1) in region_boxes]
        bot.debug(
            f"[detect] extract_coordinates: returned {len(coordinates)} per-region coords"
        )
        return coordinates

    # New behavior: return area to KEEP from (min_y + buffer) down to bottom
    min_y = min(y0 for (_, y0, _, _) in region_boxes)
    if width <= 0 or height <= 0:
        bot.warning(
            "[detect] extract_coordinates: missing Rows/Columns; cannot build keep area."
        )
        return []

    # buffer_pct is a fraction of TOTAL HEIGHT (Rows); we ADD it to min_y
    bp = float(buffer_pct) if buffer_pct is not None else 0.0
    bp = max(0.0, min(1.0, bp))
    buffer_px = int(round(bp * height))

    # start_y is the top edge of the area to KEEP (min_y moved DOWN by buffer)
    # Clamp to avoid degenerate full-frame boxes and out-of-bounds.
    start_y = min(height - 1, max(1, min_y + buffer_px))
    if start_y >= height - 1:
        bot.debug(
            "[detect] extract_coordinates: start_y near bottom → no meaningful keep area."
        )
        return []

    # CTP/DICOM: coordinates are (xmin, ymin, xmax, ymax) in pixels from top-left
    # Full width (0..Columns), rows start_y..height-1 (area to KEEP)
    keep_area = f"0,{start_y},{width},{height - 1}"
    coordinates = [keep_area]
    bot.debug(
        f"[detect] extract_coordinates: KEEP area [{keep_area}] "
        f"(min_y={min_y}, buffer_px={buffer_px})"
    )
    return coordinates
