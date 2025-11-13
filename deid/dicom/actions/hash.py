import hashlib
import os


def id_hash_sha256(dicom, **_):
    """
    - Uses an environment variable SECRET_SALT as salt
    - Uses PatientID concatenated with salt
    - Returns the first 16 hex characters of a SHA256 hash
    """
    # --- Hardcoded options ---
    salt = os.getenv("SECRET_SALT")
    if not salt:
        raise RuntimeError(
            "SECRET_SALT environment variable must be set for id_hash_sha256."
        )
    n = 16  # fixed output length

    # --- Build key string and hash ---
    patient_id = str(dicom.get("PatientID", ""))
    to_hash = salt + patient_id
    digest = hashlib.sha256(to_hash.encode("utf-8")).hexdigest()
    return digest[:n]
