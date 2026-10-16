"""Canonical display conversion for raw LOG/SUB DSA DICOMs."""

from .core import (
    PROFILE_ID,
    PresentationBatchError,
    PresentationDecision,
    PresentationError,
    PresentationResult,
    classify_presentation,
    derive_presentation_dicom,
    preprocess_series_index,
    presentation_profile_hash,
    presentation_profile_payload,
)

__version__ = "0.1.0"
__all__ = [
    "PROFILE_ID", "PresentationBatchError", "PresentationDecision",
    "PresentationError", "PresentationResult", "classify_presentation",
    "derive_presentation_dicom", "preprocess_series_index",
    "presentation_profile_hash", "presentation_profile_payload",
]
