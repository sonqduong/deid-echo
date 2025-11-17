__author__ = "Vanessa Sochat, Son Duong"
__copyright__ = "Copyright 2016-2025"
__license__ = "MIT"


import math
import os
import random
import re
from typing import Optional

import matplotlib
import numpy
from numpy.typing import NDArray
from pydicom.pixel_data_handlers.util import get_expected_length
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
        jpeg_ls: bool = True,  # hardcoded one option for compression JPEG-LS Lossless
        filename=None,
    ):
        """
        Save a cleaned dicom to disk.

        Parameters
        ----------
        jpeg_ls : bool
            If True (default), compress using JPEG-LS Lossless (1.2.840.10008.1.2.4.80)
            and rewrite file_meta.TransferSyntaxUID accordingly.
            If False, save uncompressed Explicit VR Little Endian.

        If `filename` is provided, it's used directly (no auto 'cleaned-' prefix).
        If `filename` is missing an extension or has the wrong one, '.dcm' is enforced.
        """
        if not hasattr(self, image_type):
            bot.warning("use detect() --> clean() before saving is possible.")
            return

        dicom_name = self._get_clean_name(output_folder, "dcm", filename=filename)
        dicom = utils.dcmread(self.dicom_file, force=True)

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

        original_ts = dicom.file_meta.TransferSyntaxUID
        was_compressed = getattr(original_ts, "is_compressed", False)

        # Decompress if needed
        if was_compressed:
            bot.debug(f"[clean.save] decompressing from {original_ts}")
            dicom.decompress()

        # Replace PixelData
        dicom.PixelData = getattr(self, image_type).tobytes()

        # Harmonize color metadata if pixel data is 3-channel interleaved
        arr = getattr(self, image_type)
        if arr.ndim in (3, 4) and (arr.shape[-1] == 3):  # RGB image or RGB cine
            dicom.SamplesPerPixel = 3
            dicom.PhotometricInterpretation = "RGB"
            dicom.PlanarConfiguration = 0  # interleaved by pixel
        elif arr.ndim in (2, 3):  # grayscale image or grayscale cine
            dicom.SamplesPerPixel = 1
            dicom.PhotometricInterpretation = "MONOCHROME2"
            if "PlanarConfiguration" in dicom:
                del dicom.PlanarConfiguration

        # JPEG-LS branch or uncompressed fallback
        if jpeg_ls:
            try:
                dicom.compress(_JPEGLS_LOSSLESS_UID)
                dicom.file_meta.TransferSyntaxUID = (
                    _JPEGLS_LOSSLESS_UID  # rewrite metadata
                )
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
    cleaned = None

    # Load in dicom file, and image data
    dicom = utils.load_dicom(dicom_file)
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

    # Compile coordinates from result, generate list of tuples with coordinate and value
    # keepcoordinates == 1 (included in mask) and coordinates == 0 (remove).
    coordinates = []

    for item in results["results"]:
        # We iterate through coordinates in order specified in file
        for coordinate_set in item.get("coordinates", []):
            # Each is a list with [value, coordinate]
            mask_value, new_coordinates = coordinate_set

            if not isinstance(new_coordinates, list):
                new_coordinates = [new_coordinates]

            for new_coordinate in new_coordinates:
                # Case 1: an "all" indicates applying to entire image
                if new_coordinate.lower() == "all":
                    # 2D - Greyscale Image - Shape = (X, Y) OR 3D - RGB Image - Shape = (X, Y, Channel)
                    if len(original.shape) == 2 or (
                        len(original.shape) == 3 and dicom.SamplesPerPixel == 3
                    ):
                        # minr, minc, maxr, maxc = [0, 0, Y, X]
                        new_coordinate = [
                            0,
                            0,
                            original.shape[1],
                            original.shape[0],
                        ]

                    # 4D - RGB Cine Clip - Shape = (frames, X, Y, channel) OR 3D - Greyscale Cine Clip - Shape = (frames, X, Y)
                    if len(original.shape) == 4 or (
                        len(original.shape) == 3 and dicom.SamplesPerPixel == 1
                    ):
                        new_coordinate = [
                            0,
                            0,
                            original.shape[2],
                            original.shape[1],
                        ]
                else:
                    new_coordinate = [int(x) for x in new_coordinate.split(",")]
                coordinates.append(
                    (mask_value, new_coordinate)
                )  # [(1, [1,2,3,4]),...(0, [1,2,3,4])]

    # Instead of writing directly to data, create a mask of 1s (start keeping all)
    # For 4D RGB Cine - (frames, X, Y, channel) or 3D Greyscale Cine - (frames, X, Y)
    if len(original.shape) == 4 or (
        len(original.shape) == 3 and dicom.SamplesPerPixel == 1
    ):
        mask = numpy.ones(original.shape[1:3], dtype=numpy.uint8)
    # For 2D Greyscale image (X, Y) or 3D RGB Image (X, Y channel)
    else:
        mask = numpy.ones(original.shape[0:2], dtype=numpy.uint8)

    # Here we apply the coordinates to the mask, 1==keep, 0==clean
    for coordinate_value, coordinate in coordinates:
        minr, minc, maxr, maxc = coordinate

        # Update the mask: values set to 0 to be black
        mask[minc:maxc, minr:maxr] = coordinate_value

    # Now apply finished mask to the data
    # RGB cine clip
    if len(original.shape) == 4:
        # np.tile does the copying and stacking of masks into the channel dim to produce 3D masks
        # transposition to convert tile output (channel, X, Y)  into (X, Y, channel)
        # see: https://github.com/nquach/anonymize/blob/master/anonymize.py#L154
        channel3mask = numpy.transpose(numpy.tile(mask, (3, 1, 1)), (1, 2, 0))

        # use numpy.tile to copy and stack the 3D masks into 4D array to apply to 4D pixel data
        # tile converts (X, Y, channels) -> (frames, X, Y, channels), presumed ordering for 4D pixel data
        final_mask = numpy.tile(channel3mask, (original.shape[0], 1, 1, 1))

        # apply final 4D mask to 4D pixel data
        cleaned = final_mask * original

    # RGB image or Greyscale cine clip
    elif len(original.shape) == 3:
        # This condition is ambiguous.  If the image shape is 3, we may have a single frame RGB image: size (X, Y, channel)
        # or a multiframe greyscale image: size (frames, X, Y).  Interrogate the SamplesPerPixel field.
        if dicom.SamplesPerPixel == 3:
            # RGB Image
            # Convert (X, Y) -> (X, Y, channel)
            final_mask = numpy.transpose(
                numpy.tile(mask, (original.shape[2], 1, 1)), (1, 2, 0)
            )
        else:
            # Greyscale cine clip
            # Convert (X, Y) -> (frames, X, Y)
            final_mask = numpy.tile(mask, (original.shape[0], 1, 1))

        # apply final 3D mask to 3D pixel data
        cleaned = final_mask * original

    # greyscale image: no need to stack into the channel dim since it doesn't exist
    elif len(original.shape) == 2:
        cleaned = mask * original

    else:
        bot.warning(
            "Pixel array dimension %s is not recognized." % (str(original.shape))
        )

    return cleaned
