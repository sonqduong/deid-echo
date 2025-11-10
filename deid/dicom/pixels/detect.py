__author__ = "Vanessa Sochat"
__copyright__ = "Copyright 2016-2025, Vanessa Sochat"
__license__ = "MIT"


from typing import Iterable, List, Optional, Union

from pydicom import FileDataset
from pydicom.sequence import Sequence

import deid.dicom.utils as utils
from deid.config import DeidRecipe
from deid.dicom.filter import apply_filter
from deid.logger import bot


def evaluate_group(flags):
    """
    Evaluate group will take a list of flags (e.g., [True, 'and', False, 'or', True])
    """
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
):
    """
    Determine if a dicom file has burned pixels.

    Parameters added:
    - allowed_rsf: iterable of RegionSpatialFormat integers to keep when extracting
      coordinates from SequenceOfUltrasoundRegions. Defaults to (1,2,3).
    - allowed_rdt: optional iterable of RegionDataType integers to keep (None = ignore).
    """
    if not isinstance(deid, DeidRecipe):
        if deid is None:
            deid = "dicom"
        deid = DeidRecipe(deid)

    if isinstance(dicom_files, list):
        return _has_burned_pixels_multi(
            dicom_files, force, deid, allowed_rsf=allowed_rsf, allowed_rdt=allowed_rdt
        )
    return _has_burned_pixels_single(
        dicom_files, force, deid, allowed_rsf=allowed_rsf, allowed_rdt=allowed_rdt
    )


def _has_burned_pixels_multi(
    dicom_files: List[Union[str, FileDataset]],
    force,
    deid,
    *,
    allowed_rsf: Optional[Iterable[int]],
    allowed_rdt: Optional[Iterable[int]],
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
):
    bot.debug(f"[detect] loading DICOM: {dicom_file}")
    dicom = utils.load_dicom(dicom_file, force=force)

    results = []
    global_flagged = False

    filters = deid.get_filters()
    if not filters:
        bot.warning("Deid provided does not have %filter.")
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
                        bot.debug(f"[detect]   group#{group_idx} eval: {desc} → {flag}")

                    overall_group_flag = evaluate_group(this_group_flags)
                    this_item_flags.append(overall_group_flag)
                    bot.debug(
                        f"[detect]   group#{group_idx} overall → {overall_group_flag}"
                    )

            # Evaluate the item-level result
            item_flag = evaluate_group(this_item_flags)

            # Combine with item-level operator (if present)
            operator = ""
            if isinstance(last_group, dict) and last_group.get("operator") is not None:
                operator = last_group["operator"]
                flags.append(operator)

            flags.append(item_flag)
            reason = (f"{operator} " + " ".join(group_descriptions)).replace("\n", " ")
            descriptions.append(reason)

            flagged = evaluate_group(flags=flags)
            bot.debug(
                f"[detect] item#{item_idx} item_flag={item_flag} operator={operator!r} → flagged={flagged}"
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
                            f"[detect]   coordset#{c_idx} malformed: {coordset!r}"
                        )
                        continue
                    mask_value, coord_spec = coordset
                    if isinstance(coord_spec, str) and coord_spec.startswith("from:"):
                        new_coords = extract_coordinates(
                            dicom,
                            coord_spec,
                            allowed_rsf=allowed_rsf,
                            allowed_rdt=allowed_rdt,
                        )
                        bot.debug(
                            f"[detect]   coordset#{c_idx} resolved from {coord_spec!r} "
                            f"→ {len(new_coords)} coords (allowed_rsf={allowed_rsf}, allowed_rdt={allowed_rdt})"
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


def extract_coordinates(
    dicom,
    field: str,
    *,
    allowed_rsf: Optional[Iterable[int]] = (1, 2, 3),
    allowed_rdt: Optional[Iterable[int]] = None,
):
    """
    Given a field that is provided for a dicom, extract coordinates.
    Filters SequenceOfUltrasoundRegions by RegionSpatialFormat (and optional RegionDataType).
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
    bot.debug(
        f"[detect] extract_coordinates: regions={len(regions)}, rsf_allow={rsf_set}, rdt_allow={rdt_set}"
    )

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
            bot.debug(f"[detect]   region#{i} skip rsf={rsf_i}")
            continue
        if rdt_set is not None and rdt_i not in rdt_set:
            bot.debug(f"[detect]   region#{i} skip rdt={rdt_i}")
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
            coordinates.append(f"{xmin},{ymin},{xmax},{ymax}")
            bot.debug(
                f"[detect]   region#{i} add coord [{xmin},{ymin},{xmax},{ymax}] (rsf={rsf_i}, rdt={rdt_i})"
            )
        else:
            bot.debug(f"[detect]   region#{i} missing location tags; skipping")

    bot.debug(f"[detect] extract_coordinates: returned {len(coordinates)} coords")
    return coordinates
