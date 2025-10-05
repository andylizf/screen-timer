"""Utilities for working with CMSampleBuffer and converting to image bytes."""

import logging
from typing import Optional

from AppKit import (  # type: ignore
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
)
from Quartz import (  # type: ignore
    CVPixelBufferGetHeight,
    CVPixelBufferGetWidth,
)

from Quartz import CIContext, CIImage  # type: ignore

import CoreMedia  # type: ignore

_CI_CONTEXT = CIContext.contextWithOptions_(None)


def sample_buffer_to_png(sample_buffer) -> Optional[bytes]:
    """Convert a `CMSampleBufferRef` to PNG bytes."""

    if not CoreMedia.CMSampleBufferIsValid(sample_buffer):
        return None

    pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
    if pixel_buffer is None:
        return None

    width = CVPixelBufferGetWidth(pixel_buffer)
    height = CVPixelBufferGetHeight(pixel_buffer)
    if width == 0 or height == 0:
        return None

    try:
        ci_image = CIImage.imageWithCVImageBuffer_(pixel_buffer)
        extent = ci_image.extent()
        cg_image = _CI_CONTEXT.createCGImage_fromRect_(ci_image, extent)
        bitmap = NSBitmapImageRep.alloc().initWithCGImage_(cg_image)
        data = bitmap.representationUsingType_properties_(
            NSBitmapImageFileTypePNG, None
        )
        return bytes(data) if data is not None else None
    except Exception:  # pragma: no cover
        logging.exception("Failed to convert sample buffer to PNG")
        return None
