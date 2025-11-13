__author__ = "Vanessa Sochat, Son Duong"
__copyright__ = "Copyright 2016-2025"
__license__ = "MIT"

import hashlib
import os

from deid.logger import bot
from deid.utils import get_timestamp

# Timestamps


def jitter_timestamp_func(item, value, field, **kwargs):
    """
    A wrapper to apply a deterministic jitter to a timestamp so it works as a custom function.
    """
    dataset = kwargs.get("dicom")
    jitter_days = jitter_timestamp(field, dataset)
    return _apply_jitter(field, jitter_days)


def jitter_timestamp(field, dicom=None):
    """
    Return a deterministic jitter offset in days between -365 and 365, excluding zero,
    derived from the PatientID hash.
    """
    patient_id = ""
    if dicom is not None:
        if hasattr(dicom, "get"):
            patient_id = dicom.get("PatientID", "") or patient_id
        if not patient_id and hasattr(dicom, "PatientID"):
            patient_id = dicom.PatientID or ""
    salt = os.getenv("SECRET_SALT")
    if not salt:
        raise RuntimeError(
            "SECRET_SALT environment variable must be set for jitter_timestamp."
        )
    hash_source = f"{salt}|{patient_id}".encode("utf-8")
    hash_int = int(hashlib.sha256(hash_source).hexdigest(), 16)
    day_offset = (hash_int % 365) + 1
    if hash_int & 1:
        day_offset = -day_offset
    return day_offset


def _apply_jitter(field, value):
    """
    Apply a jitter offset to a DICOM timestamp field.
    """
    if not isinstance(value, int):
        value = int(value)

    original = field.element.value
    new_value = original

    if original is not None:
        new_value = None
        dcmvr = field.element.VR

        if dcmvr == "DA":
            new_value = get_timestamp(original, jitter_days=value, format="%Y%m%d")

        elif dcmvr == "DT":
            try:
                new_value = get_timestamp(
                    original, jitter_days=value, format="%Y%m%d%H%M%S.%f%z"
                )
            except Exception:
                new_value = get_timestamp(
                    original, jitter_days=value, format="%Y%m%d%H%M%S.%f"
                )

        else:
            for fmtstr in ["%Y%m%d", "%Y%m%d%H%M%S.%f%z", "%Y%m%d%H%M%S.%f"]:
                try:
                    new_value = get_timestamp(
                        original, jitter_days=value, format=fmtstr
                    )
                    break
                except Exception:
                    pass

            if not new_value:
                bot.warning("JITTER not supported for %s with VR=%s" % (field, dcmvr))

    return new_value
