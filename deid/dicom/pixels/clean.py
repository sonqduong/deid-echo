__author__ = "Vanessa Sochat, Son Duong"
__copyright__ = "Copyright 2016-2025"
__license__ = "MIT"


import math
import os
import random
import re
from typing import List, Optional, Tuple

import matplotlib
import numpy
from numpy.typing import NDArray
from pydicom.pixel_data_handlers.util import apply_color_lut, get_expected_length
from pydicom.uid import UID, ExplicitVRLittleEndian

from deid.config import DeidRecipe
from deid.dicom import utils
from deid.logger import bot
from deid.utils import get_temporary_name

matplotlib.use("pdf")

from matplotlib import pyplot as plt  # noqa

bot.level = 3

# JPEG-LS Lossless Transfer Syntax UID (DICOM: 1.2.840.10008.1.2.4.80)
_JPEGLS_LOSSLESS_UID = UID("1.2.840.10008.1.2.4.80")
_US_MULTIFRAME_SOP_CLASS_UID = UID("1.2.840.10008.5.1.4.1.1.3.1")
_US_SINGLE_FRAME_SOP_CLASS_UID = UID("1.2.840.10008.5.1.4.1.1.6.1")


def _mask_shape_from_pixel_array(
    original: NDArray, samples_per_pixel: int
) -> Tuple[int, int]:
    """
    Return the 2D mask shape as (rows, columns) for a pydicom pixel_array.
    """
    if len(original.shape) == 4 or (
        len(original.shape) == 3 and samples_per_pixel == 1
    ):
        return int(original.shape[1]), int(original.shape[2])
    return int(original.shape[0]), int(original.shape[1])


def _coordinates_from_results(
    results: dict, rows: int, columns: int
) -> List[Tuple[int, List[int]]]:
    """
    Convert deid pixel-cleaning results to coordinate tuples.

    Coordinates use the recipe/DICOM convention (xmin, ymin, xmax, ymax).
    """
    coordinates = []

    for item in results["results"]:
        for coordinate_set in item.get("coordinates", []):
            mask_value, new_coordinates = coordinate_set

            if not isinstance(new_coordinates, list):
                new_coordinates = [new_coordinates]

            for new_coordinate in new_coordinates:
                if (
                    isinstance(new_coordinate, str)
                    and new_coordinate.lower() == "all"
                ):
                    new_coordinate = [0, 0, columns, rows]
                elif isinstance(new_coordinate, str):
                    new_coordinate = [int(x) for x in new_coordinate.split(",")]
                else:
                    new_coordinate = [int(x) for x in new_coordinate]
                coordinates.append((int(mask_value), new_coordinate))

    return coordinates


def build_mask_from_results(results: dict, rows: int, columns: int) -> NDArray:
    """
    Build the 2D keep-mask used for pixel cleaning.

    Mask value 1 means keep the original pixel. Mask value 0 means redact/black.
    """
    coordinates = _coordinates_from_results(results, rows, columns)

    if results.get("flagged") and not coordinates:
        raise RuntimeError(
            "Pixel cleaning aborted: detect() flagged file but no valid masking "
            "coordinates were produced after filtering."
        )

    mask = numpy.ones((rows, columns), dtype=numpy.uint8)
    for coordinate_value, coordinate in coordinates:
        minr, minc, maxr, maxc = coordinate
        mask[minc:maxc, minr:maxr] = coordinate_value
    return mask


def _mask_view_for_pixels(
    mask: NDArray, original: NDArray, samples_per_pixel: int
) -> NDArray:
    """
    Return a broadcastable view of a 2D mask for the given pixel array shape.
    """
    if len(original.shape) == 4:
        return mask[None, :, :, None]

    if len(original.shape) == 3:
        if samples_per_pixel == 3:
            return mask[:, :, None]
        return mask[None, :, :]

    if len(original.shape) == 2:
        return mask

    raise ValueError(f"Unsupported pixel array shape for masking: {original.shape}")


def _pixel_array_for_cleaning(
    dicom,
    *,
    fix_interpretation: bool,
    pixel_data_attribute: str,
) -> Tuple[NDArray, int]:
    pixel_data = getattr(dicom, pixel_data_attribute)

    # Get expected and actual length of the pixel data (bytes, expected does not include trailing null byte)
    expected_length = get_expected_length(dicom)
    actual_length = len(pixel_data)
    full_length = expected_length / 2 * 3  # upsampled data is a third larger
    full_length += 1 if full_length % 2 else 0  # trailing padding byte if even length

    # If we have YBR_FULL_2, must be RGB to obtain pixel data
    if (
        not dicom.file_meta.TransferSyntaxUID.is_compressed
        and dicom.PhotometricInterpretation == "YBR_FULL_422"
        and fix_interpretation
        and actual_length >= full_length
    ):
        bot.warning(
            "Updating dicom.PhotometricInterpretation to RGB, set fix_interpretation to False to skip."
        )
        photometric_original = dicom.PhotometricInterpretation
        dicom.PhotometricInterpretation = "RGB"
        original = dicom.pixel_array
        dicom.PhotometricInterpretation = photometric_original
    else:
        original = dicom.pixel_array

    # ---- local PI/SPP (do not rely on header after conversions) ----
    spp = int(getattr(dicom, "SamplesPerPixel", 1) or 1)
    pi = str(getattr(dicom, "PhotometricInterpretation", "") or "")

    # ---- PALETTE COLOR -> RGB (before masking) ----
    if fix_interpretation and pi == "PALETTE COLOR":
        try:
            # If already RGB-like, do nothing (but update local spp)
            if (
                isinstance(original, numpy.ndarray)
                and original.ndim >= 3
                and original.shape[-1] == 3
            ):
                spp = 3
            else:
                # Single-frame indexed: (rows, cols) -> (rows, cols, 3)
                if isinstance(original, numpy.ndarray) and original.ndim == 2:
                    original = apply_color_lut(original, dicom)
                    spp = 3

                # Multi-frame indexed cine: (frames, rows, cols) -> (frames, rows, cols, 3)
                elif (
                    isinstance(original, numpy.ndarray)
                    and original.ndim == 3
                    and spp == 1
                ):
                    rgb_frames = [
                        apply_color_lut(original[f], dicom)
                        for f in range(original.shape[0])
                    ]
                    original = numpy.stack(rgb_frames, axis=0)
                    spp = 3
                else:
                    bot.warning(
                        f"PALETTE COLOR unexpected pixel_array shape={getattr(original,'shape',None)}; proceeding without LUT."
                    )
            bot.warning("Converted PALETTE COLOR -> RGB for masking.")
        except Exception as e:
            bot.warning(
                f"Failed PALETTE COLOR -> RGB conversion; proceeding without LUT. err={e!r}"
            )

    return original, spp


def _apply_mask_to_pixel_array(
    original: NDArray,
    results: dict,
    samples_per_pixel: int,
    *,
    in_place: bool,
) -> Optional[NDArray]:
    mask_rows, mask_columns = _mask_shape_from_pixel_array(original, samples_per_pixel)
    mask = build_mask_from_results(results, mask_rows, mask_columns)

    if len(original.shape) not in (2, 3, 4):
        bot.warning(
            "Pixel array dimension %s is not recognized." % (str(original.shape))
        )
        return None

    cleaned = original if in_place else original.copy()
    if in_place and hasattr(cleaned, "flags") and not cleaned.flags.writeable:
        cleaned = original.copy()

    try:
        cleaned *= _mask_view_for_pixels(mask, original, samples_per_pixel)
    except ValueError:
        if not in_place:
            raise
        cleaned = original.copy()
        cleaned *= _mask_view_for_pixels(mask, original, samples_per_pixel)
    return cleaned


def _save_cleaned_array_to_dicom(
    dicom,
    arr: NDArray,
    dicom_name: str,
    *,
    jpeg_ls: bool,
    orig_pi: str,
) -> str:
    original_ts = dicom.file_meta.TransferSyntaxUID
    was_compressed = getattr(original_ts, "is_compressed", False)

    # Decompress if needed
    if was_compressed:
        bot.debug(f"[clean.save] decompressing from {original_ts}")
        dicom.decompress(generate_instance_uid=False)

    # Replace PixelData as late as possible, because tobytes() makes a full copy.
    dicom.PixelData = numpy.ascontiguousarray(arr).tobytes()

    # Harmonize color/grayscale metadata
    if arr.ndim in (3, 4) and (arr.shape[-1] == 3):  # RGB image or RGB cine
        dicom.SamplesPerPixel = 3
        dicom.PhotometricInterpretation = "RGB"
        dicom.PlanarConfiguration = 0  # interleaved by pixel

        # If the source used a palette LUT, remove it now that we are true RGB
        palette_tags = [
            "RedPaletteColorLookupTableDescriptor",
            "GreenPaletteColorLookupTableDescriptor",
            "BluePaletteColorLookupTableDescriptor",
            "RedPaletteColorLookupTableData",
            "GreenPaletteColorLookupTableData",
            "BluePaletteColorLookupTableData",
            "SegmentedRedPaletteColorLookupTableData",
            "SegmentedGreenPaletteColorLookupTableData",
            "SegmentedBluePaletteColorLookupTableData",
            "PaletteColorLookupTableUID",
        ]
        for name in palette_tags:
            if name in dicom:
                del dicom[name]

    elif arr.ndim in (
        2,
        3,
    ):  # grayscale image or grayscale cine (or palette indices if not converted)
        dicom.SamplesPerPixel = 1

        # If source was PALETTE COLOR but we still have 2D/3D indexed data,
        # DO NOT relabel as MONOCHROME2 (that destroys meaning).
        if orig_pi == "PALETTE COLOR":
            dicom.PhotometricInterpretation = "PALETTE COLOR"
        else:
            dicom.PhotometricInterpretation = "MONOCHROME2"

        if "PlanarConfiguration" in dicom:
            del dicom.PlanarConfiguration

    # Align bit-depth fields to dtype (applies to RGB and grayscale)
    if numpy.issubdtype(arr.dtype, numpy.integer):
        bits = int(arr.dtype.itemsize * 8)  # uint8 -> 8, uint16 -> 16
        dicom.BitsAllocated = bits
        dicom.BitsStored = bits
        dicom.HighBit = bits - 1
        dicom.PixelRepresentation = (
            1 if numpy.issubdtype(arr.dtype, numpy.signedinteger) else 0
        )

    sop_class_uid = str(
        getattr(dicom, "SOPClassUID", getattr(dicom, "SOP_CLASS_UID", "")) or ""
    )
    if sop_class_uid == str(_US_MULTIFRAME_SOP_CLASS_UID):
        is_multiframe = True
    elif sop_class_uid == str(_US_SINGLE_FRAME_SOP_CLASS_UID):
        is_multiframe = False
    else:
        # Fall back to the cleaned array shape when SOP Class UID is absent or
        # not one of the ultrasound storage classes we special-case.
        is_multiframe = arr.ndim == 4 or (arr.ndim == 3 and arr.shape[-1] != 3)

    # JPEG-LS branch or uncompressed fallback. Still frames are always written
    # uncompressed for downstream viewer compatibility.
    if jpeg_ls and is_multiframe:
        try:
            dicom.compress(_JPEGLS_LOSSLESS_UID, generate_instance_uid=False)
            dicom.file_meta.TransferSyntaxUID = _JPEGLS_LOSSLESS_UID
            bot.debug(
                f"[clean.save] recompressed using JPEG-LS Lossless tsuid={_JPEGLS_LOSSLESS_UID}"
            )
        except NotImplementedError:
            bot.warning(
                "[clean.save] JPEG-LS Lossless compression not available; saving Explicit VR Little Endian."
            )
            dicom.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            bot.debug(
                f"[clean.save] writing uncompressed tsuid={dicom.file_meta.TransferSyntaxUID}"
            )
    else:
        dicom.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        bot.debug(
            f"[clean.save] writing uncompressed tsuid={dicom.file_meta.TransferSyntaxUID}"
        )

    dicom.save_as(dicom_name)
    bot.debug(f"[clean.save] wrote: {dicom_name}")
    return dicom_name


class DicomCleaner:
    """
    Clean a dicom file of burned pixels.

    take an input dicom file, check for burned pixels, and then clean,
    with option to save / output in multiple formats. This object should
    map to one dicom file, and the usage flow is the following:
    cleaner = DicomCleaner()
    summary = cleaner.detect(dicom_file)

    cleaner.clean()
    """

    def __init__(
        self,
        output_folder=None,
        add_padding=False,
        margin=3,
        deid=None,
        font=None,
        force=True,
    ):
        if output_folder is None:
            output_folder = get_temporary_name(prefix="clean")

        if font is None:
            font = self.default_font()
        self.font = font
        self.cmap = "gray"
        self.output_folder = output_folder
        self.recipe = DeidRecipe(deid)
        self.results = None
        self.force = force
        self.dicom_file: Optional[str] = None
        self.cleaned: Optional[NDArray] = None

    def default_font(self):
        """
        Get the default font to use for a title.

        define the font style for saving png figures
        if a title is provided
        """
        return {"family": "serif", "color": "darkred", "weight": "normal", "size": 16}

    def detect(self, dicom_file, **kwargs):
        """
        Initiate the cleaner for a new dicom file.

        Pass-through **kwargs lets you supply allowed_rsf / allowed_rdt, e.g.:
        cleaner.detect(path, allowed_rsf={1,2,3}, allowed_rdt={1,2,4})
        """
        from deid.dicom.pixels.detect import has_burned_pixels

        self.results = has_burned_pixels(
            dicom_file, deid=self.recipe.deid, force=self.force, **kwargs
        )
        self.dicom_file = dicom_file
        return self.results

    def clean(
        self, fix_interpretation: bool = True, pixel_data_attribute: str = "PixelData"
    ) -> Optional[NDArray]:
        if not self.results:
            bot.warning(
                "Use %s.detect() with a dicom file to find coordinates first." % self
            )
            return

        bot.info("Scrubbing %s." % self.dicom_file)
        self.cleaned = clean_pixel_data(
            dicom_file=self.dicom_file,
            results=self.results,
            fix_interpretation=fix_interpretation,
            pixel_data_attribute=pixel_data_attribute,
        )
        return self.cleaned

    def get_figure(self, show=False, image_type="cleaned", title=None):
        """
        Get a figure for an original or cleaned image.

        If the image was already clean, it is simply a copy of the original.
        If show is True, plot the image. If a 4d image is discovered, we use
        randomly choose a slice.
        """
        if hasattr(self, image_type):
            _, ax = plt.subplots(figsize=(10, 6))

            # Retrieve full image
            image = getattr(self, image_type)

            # Handle 4d data by choosing one dimension
            if len(image.shape) == 4:
                channel = random.choice(range(image.shape[3]))
                bot.warning(
                    "Image detected as 4d, will sample channel %s and middle slice"
                    % channel
                )
                image = image[math.floor(image.shape[0] / 2), :, :, channel]

            ax.imshow(image, cmap=self.cmap)
            if title is not None:
                plt.title(title, fontdict=self.font)
            if show is True:
                plt.show()
            return plt

    def _get_clean_name(self, output_folder, extension="dcm", filename=None):
        """
        Get path to an output file.

        If `filename` is provided, use it as-is (no auto 'cleaned-' prefix), ensuring
        the expected extension. If `filename` is not provided, fall back to the
        original behavior that prefixes with 'cleaned-'.

        Parameters
        ==========
        output_folder: the output folder to create, will be created if doesn't exist.
        extension: the extension of the file to create a name for, should not start with "."
        filename: optional explicit filename or path to use for saving (no auto-suffix).
        """
        if output_folder is None:
            output_folder = self.output_folder

        expected_ext = "." + extension.lstrip(".")

        if filename:
            target = filename
            # If filename is not an absolute path and doesn't include a separator, join with output_folder
            if not os.path.isabs(target) and os.sep not in target:
                if not os.path.exists(output_folder):
                    bot.debug("Creating output folder %s" % output_folder)
                    os.makedirs(output_folder)
                target = os.path.join(output_folder, filename)
            else:
                parent = os.path.dirname(target)
                if parent and not os.path.exists(parent):
                    bot.debug("Creating parent folder %s" % parent)
                    os.makedirs(parent)

            root, ext_in = os.path.splitext(target)
            if not ext_in:
                target = root + expected_ext
            elif ext_in.lower() != expected_ext.lower():
                bot.warning(
                    "Replacing file extension %s with %s for output format."
                    % (ext_in, expected_ext)
                )
                target = root + expected_ext
            return target

        if not os.path.exists(output_folder):
            bot.debug("Creating output folder %s" % output_folder)
            os.makedirs(output_folder)

        basename = re.sub("[.]dicom|[.]dcm", "", os.path.basename(self.dicom_file))
        return "%s/cleaned-%s.%s" % (output_folder, basename, extension)

    def save_dicom(
        self,
        output_folder=None,
        image_type="cleaned",
        jpeg_ls: bool = True,
        filename=None,
    ):
        """
        Save a cleaned dicom to disk.

        Parameters
        ----------
        jpeg_ls : bool
            If True (default), multi-frame outputs are compressed using JPEG-LS
            Lossless (1.2.840.10008.1.2.4.80). Still-frame outputs are always
            saved uncompressed as Explicit VR Little Endian. If False, all
            outputs are saved uncompressed.

        If `filename` is provided, it's used directly (no auto 'cleaned-' prefix).
        If `filename` is missing an extension or has the wrong one, '.dcm' is enforced.
        """
        if not hasattr(self, image_type):
            bot.warning("use detect() --> clean() before saving is possible.")
            return

        dicom_name = self._get_clean_name(output_folder, "dcm", filename=filename)
        dicom = utils.dcmread(self.dicom_file, force=True)

        # ---- capture original PI before any modifications ----
        orig_pi = str(getattr(dicom, "PhotometricInterpretation", "") or "")

        # Log current pixel/meta
        try:
            shape = getattr(self, image_type).shape
        except Exception:
            shape = None
        bot.debug(
            f"[clean.save] source tsuid={dicom.file_meta.TransferSyntaxUID}, "
            f"PI={getattr(dicom,'PhotometricInterpretation',None)}, "
            f"SPP={getattr(dicom,'SamplesPerPixel',None)}, "
            f"Rows={getattr(dicom,'Rows',None)}, Cols={getattr(dicom,'Columns',None)}, "
            f"cleaned_shape={shape}"
        )

        return _save_cleaned_array_to_dicom(
            dicom,
            getattr(self, image_type),
            dicom_name,
            jpeg_ls=jpeg_ls,
            orig_pi=orig_pi,
        )


def clean_pixel_data(
    dicom_file,
    results: dict,
    fix_interpretation: bool = True,
    pixel_data_attribute: str = "PixelData",
):
    """
    Clean a dicom file.

    take a dicom image and a list of pixel coordinates, and return
    a cleaned file (if output file is specified) or simply plot
    the cleaned result (if no file is specified)

    Parameters
    ==========
    dicom_file: (str or FileDataset instance) Dicom file to clean
    results: Result of the .has_burned_pixels() method
    fix_interpretation: fix the photometric interpretation if found off
    pixel_data_attribute: PixelData attribute name in the dicom file
    """
    # Load in dicom file, and image data
    dicom = utils.load_dicom(dicom_file)
    original, spp = _pixel_array_for_cleaning(
        dicom,
        fix_interpretation=fix_interpretation,
        pixel_data_attribute=pixel_data_attribute,
    )
    return _apply_mask_to_pixel_array(original, results, spp, in_place=False)


def clean_pixel_data_to_file(
    dicom_file,
    results: dict,
    output_file,
    *,
    jpeg_ls: bool = True,
    force: bool = True,
    fix_interpretation: bool = True,
    pixel_data_attribute: str = "PixelData",
) -> Optional[str]:
    """
    Clean and save a DICOM using one dataset read.

    This is the low-memory pipeline path: decode pixels, mask in place when the
    decoder provides a writeable array, assign PixelData late, then save.
    """
    dicom = utils.load_dicom(dicom_file, force=force)
    orig_pi = str(getattr(dicom, "PhotometricInterpretation", "") or "")
    original, spp = _pixel_array_for_cleaning(
        dicom,
        fix_interpretation=fix_interpretation,
        pixel_data_attribute=pixel_data_attribute,
    )
    cleaned = _apply_mask_to_pixel_array(original, results, spp, in_place=True)
    if cleaned is None:
        return None
    return _save_cleaned_array_to_dicom(
        dicom,
        cleaned,
        str(output_file),
        jpeg_ls=jpeg_ls,
        orig_pi=orig_pi,
    )
