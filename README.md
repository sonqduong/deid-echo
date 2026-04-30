# deid-echo

**Echo-focused DICOM de-identification for ultrasound studies**

`deid-echo` is a fork of [`pydicom/deid`](https://github.com/pydicom/deid) that is
narrowed, tuned, and validated specifically for **echocardiogram ultrasound
DICOM workflows**. It includes echo-specific recipes, ultrasound region handling,
and parallelized runners designed for large-scale processing.

**Important**
This software has been developed and evaluated **only on echocardiogram
ultrasound data**. Use on other modalities is not supported.

---

## About this fork

This repository starts from `pydicom/deid` and makes targeted changes for
echocardiography workflows:

- **Echocardiogram-specific defaults**
  - Recipes and helpers tuned for ultrasound studies
  - Pixel cleaning respects `SequenceOfUltrasoundRegions`
- **Parallelized batch execution**
  - Multi-process runners in `deidecho_run/` (e.g. `run_echodeid.py`)
  - Designed for very large echo cohorts
- **Intentionally narrow scope**
  - This is not a general-purpose DICOM de-identification toolkit

---

## Installation

`deid-echo` must be installed from source (either from a local clone or directly from GitHub).

### Create an environment

```bash
conda env create -f environment.yml
conda activate deid-echo
```

### Option A (recommended): install from a local clone

```bash
cd /path/to/workingdirectory  # choose where you want the repo checked out
git clone https://github.com/sonqduong/deid-echo.git
cd deid-echo
pip install -e .
```

### Option B: install directly from GitHub

```bash
cd /path/to/workingdirectory
pip install git+https://github.com/sonqduong/deid-echo.git
```

---

## Data setup

- Place **all input echocardiogram DICOM files** under a **single input directory**.
- Provide a **separate output directory** (not a subfolder of the input directory) where de-identified DICOMs will be written.
- Provide a path to the recipe (default recipe lives in `deidecho_run/deidecho_recipe`).

> Note: Because metadata is used for hashing, it is best **not** to pre-clean metadata.
> This tool is built to work on files as they are stored natively in the PACS.

### SALT / secret passphrase

`deid-echo` deterministically hashes/jitters the following fields using a center-specific secret passphrase (salt) plus embedded metadata:

- `PatientID`
- `StudyInstanceUID` (and related UIDs)
- `PatientBirthDate`
- `StudyDate`

Provide the salt passphrase either:

- via environment variable: `SECRET_SALT`, or
- via CLI: `--salt`

---

## Running

```bash
conda activate deid-echo  # or however you activate your environment
cd /path/to/workingdirectory/deid-echo/deidecho_run

#for linux/mac
python run_echodeid.py \
  --input-root /path/to/originaldicomfiles \
  --output-root /path/to/deiddicomfiles \
  --recipe-path deidecho_recipe \
  --salt 123

#for windows (powershell)
 python run_echodeid.py `
  --input-root "C:\path\to\originaldicomfiles" `
  --output-root "C:\path\to\deiddicomfiles" `
  --recipe-path "deidecho_recipe" `
  --salt 123
```

Notes The defaults are set to run on a smaller computer without hitting memory limits. There are several other command line arguments provided to speed up processing.  These will increase RAM needs.  Review the logs-- if some long acquistions seem to be erroring due to out of memory, then adjust these knobs. 

 
- Like the original `deid`, this is driven by a **Recipe** (provided in
  `deidecho_run/deidecho_recipe`).
- **Metadata header rewrites**

  - Specialized functions were developed to create *deterministic* hashing of
    UIDs and deterministic date jittering (±365 days), based on a
    center-specific secret passphrase and embedded metadata. This allows data
    to be de-identified in a non-random fashion so that relationships between
    person, study, series, instance, and time are preserved *within a given
    individual*.
  - All PHI-related metadata tags relevant to echocardiogram ultrasound are
    identified and removed (and can be adjusted as needed in the recipe).
  - The header-cleaned DICOM file is saved **separately** to the following
    directory structure:

    hashedPatientID/hashedStudyUID/SeriesNumber/InstanceNumber.dcm

    The original data are never overwritten. A new file is always written to
    a new path, which is important when metadata identifiers are embedded in
    the original filename.
- **Pixel cleaning** is performed on the newly header-cleaned DICOM file.

  - The original `deid` recipe followed the CTP de-identification protocol,
    which prespecifies coordinates to black out based on ultrasound machine
    make and model (for example, the top “banner” containing patient names).
    We observed that this approach did not reliably remove PHI in the initial
    “splash screen” clip and that acquisition date/time could occasionally be
    displayed in regions that were not blanked.
  - A modified recipe was introduced that instead used metadata tags to
    identify bounding boxes of regions of interest to black out. However, we
    found that this approach could obscure clinically relevant regions, such
    as the scale bar on spectral Doppler traces.
  - The solution implemented here constructs bounding boxes based on the
    **top-most coordinates** of relecant **Tissue, Color, or Doppler data** as
    defined by metadata tags. This approach was found to preserve the maximum
    amount of useful image content while ensuring no PHI leakage.
- **Support for parallelization** is provided for large-scale processing.
- A **CSV log** of the de-identification process is generated, including:

  - original PHI values,
  - recoded/hashed values,
  - the new path to the de-identified file, and
  - any filters applied or errors encountered.
    Additional logs are created during parallel execution that allow the process
    to be resumed if it crashes.

### High Performance Computing Usage
Increase these settings to obtain faster batch processing:
--workers: 40 \
--chunksize: 32 \
--max-tasks-per-child: 50 \
--pixelmed-concurrency: 24 \
--pixelmed-frame-batch-size: 32 \
--pixelmed-java-xmx: 1g \
--flush-every: 1000 \

(pixelmed-concurency note: unset by default, and resolves to max workers: big memory consumer)

**Known gotchas:**

- A built-in filter restricts processing to SOPClassUIDs corresponding to
  ultrasound and ultrasound multi-frame images. All other SOP classes are
  skipped.
- If bounding box coordinates are not supplied in the metadata, the file is
  skipped, as these tags are required for the de-identification process.
- To our knowledge, 3D volume data are skipped and not processed, as they do not
  contain the standard metadata tags required for de-identification.
