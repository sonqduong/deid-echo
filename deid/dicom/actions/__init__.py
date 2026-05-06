from .hash import id_hash_sha256
from .jitter import (
    jitter_birthdate_cap_89_func,
    jitter_timestamp,
    jitter_timestamp_func,
)
from .uids import basic_uuid, dicom_uuid, pydicom_uuid, salted_pydicom_uuid, suffix_uuid

# Function lookup
# Functions here must take an item, field, and value

deid_funcs = {
    "jitter": jitter_timestamp_func,
    "jitter_birthdate_cap_89": jitter_birthdate_cap_89_func,
    "id_hash": id_hash_sha256,
    "dicom_uuid": dicom_uuid,
    "suffix_uuid": suffix_uuid,
    "basic_uuid": basic_uuid,
    "pydicom_uuid": pydicom_uuid,
    "pydicom_uuid_salt": salted_pydicom_uuid,
}
