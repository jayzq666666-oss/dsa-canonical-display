from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, XRayAngiographicImageStorage, generate_uid

from dsa_canonical_display.core import (
    NEUTRAL_VALUE,
    PresentationBatchError,
    _edge_preserving_noise_filter,
    _map_to_display,
    classify_presentation,
    derive_presentation_dicom,
    preprocess_series_index,
)


def test_v2_soft_mapping_does_not_force_observed_values_to_container_extremes():
    delta = np.array([[-300.0, -20.0, 0.0, 20.0, 80.0]], dtype=np.float32)
    mapped = _map_to_display(delta)
    assert int(mapped[0, 2]) == NEUTRAL_VALUE
    assert 0 < int(mapped.min()) < NEUTRAL_VALUE
    assert NEUTRAL_VALUE < int(mapped.max()) < 4095
    assert np.all(np.diff(mapped.astype(np.int32), axis=1) > 0)


def test_v2_noise_filter_reduces_background_noise_and_preserves_strong_edge():
    rng = np.random.default_rng(7)
    delta = rng.normal(0, 8, size=(96, 96)).astype(np.float32)
    delta[:, 48:] -= 120
    filtered = _edge_preserving_noise_filter(delta)
    raw_noise = float(np.std(delta[12:84, 12:40]))
    filtered_noise = float(np.std(filtered[12:84, 12:40]))
    raw_edge = float(np.mean(delta[:, 47] - delta[:, 48]))
    filtered_edge = float(np.mean(filtered[:, 47] - filtered[:, 48]))
    assert filtered_noise < raw_noise * 0.75
    assert filtered_edge > raw_edge * 0.80


def _write_dicom(path: Path, pixels: np.ndarray, *, relationship: str = "LOG", mode: str = "SUB", mask: int = 1) -> FileDataset:
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = XRayAngiographicImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = XRayAngiographicImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.Modality = "XA"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "SINGLE PLANE", "SINGLE A"]
    ds.Rows = pixels.shape[1]
    ds.Columns = pixels.shape[2]
    ds.NumberOfFrames = pixels.shape[0]
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    ds.PixelIntensityRelationship = relationship
    ds.RecommendedViewingMode = mode
    item = Dataset()
    item.MaskOperation = "AVG_SUB"
    item.MaskFrameNumbers = mask
    ds.MaskSubtractionSequence = Sequence([item])
    ds.FrameTime = 66.7
    ds.PixelData = pixels.astype("<u2").tobytes()
    ds.save_as(path, enforce_file_format=True)
    return ds


def test_classification_is_fail_closed_for_invalid_subtraction(tmp_path: Path):
    pixels = np.full((2, 4, 4), 1000, dtype=np.uint16)
    path = tmp_path / "source.dcm"
    ds = _write_dicom(path, pixels)
    assert classify_presentation(ds).action == "DERIVE"
    ds.MaskSubtractionSequence[0].MaskFrameNumbers = 9
    assert classify_presentation(ds).action == "UNSUPPORTED"
    ds.PixelIntensityRelationship = "DISP"
    assert classify_presentation(ds).action == "PASSTHROUGH"


def test_derive_maps_mask_to_neutral_and_vessels_dark(tmp_path: Path):
    mask = np.full((8, 8), 2000, dtype=np.uint16)
    contrast = mask.copy()
    contrast[2:6, 3:5] = 1200
    bright = mask.copy()
    bright[0:2, 0:2] = 2500
    source = tmp_path / "source.dcm"
    output = tmp_path / "derived.dcm"
    original = _write_dicom(source, np.stack([mask, contrast, bright]))

    first = derive_presentation_dicom(source, output)
    second = derive_presentation_dicom(source, output)
    derived = pydicom.dcmread(output)
    decoded = derived.pixel_array

    assert first == second
    assert np.all(decoded[0] == NEUTRAL_VALUE)
    assert decoded[1, 3, 3] < NEUTRAL_VALUE
    assert decoded[2, 0, 0] > NEUTRAL_VALUE
    assert int(decoded.max()) < 4095
    assert int(derived.BitsStored) == 12
    assert str(derived.file_meta.TransferSyntaxUID) == "1.2.840.10008.1.2.5"
    assert str(derived.PixelIntensityRelationship) == "DISP"
    assert "MaskSubtractionSequence" not in derived
    assert str(derived.StudyInstanceUID) == str(original.StudyInstanceUID)
    assert str(derived.SeriesInstanceUID) != str(original.SeriesInstanceUID)
    assert str(derived.SOPInstanceUID) != str(original.SOPInstanceUID)


def test_preprocess_index_rewrites_only_derivable_paths(tmp_path: Path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    raw = source_root / "raw.dcm"
    ready = source_root / "ready.dcm"
    pixels = np.stack([
        np.full((4, 4), 2000, dtype=np.uint16),
        np.full((4, 4), 1500, dtype=np.uint16),
    ])
    _write_dicom(raw, pixels)
    _write_dicom(ready, pixels, relationship="DISP", mode="SUB")
    raw_index = tmp_path / "raw.csv"
    with raw_index.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["study_uid", "series_uid", "source_path"])
        writer.writeheader()
        writer.writerow({"study_uid": "study", "series_uid": "series", "source_path": f"{raw};{ready}"})

    effective = tmp_path / "effective.csv"
    summary = preprocess_series_index(
        raw_index,
        effective,
        source_root=source_root,
        derived_root=tmp_path / "derived",
        audit_csv=tmp_path / "audit.csv",
        summary_json=tmp_path / "summary.json",
        workers=2,
    )
    row = next(csv.DictReader(effective.open(encoding="utf-8-sig")))
    paths = row["source_path"].split(";")
    assert paths[0].endswith("raw.display-v2.dcm")
    assert paths[1] == str(ready.resolve())
    assert summary["counts"] == {"DERIVED": 1, "PASSTHROUGH": 1}


def test_preprocess_index_does_not_publish_partial_index_on_failure(tmp_path: Path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    bad = source_root / "bad.dcm"
    pixels = np.stack([np.full((4, 4), 1000, dtype=np.uint16)] * 2)
    ds = _write_dicom(bad, pixels)
    ds.MaskSubtractionSequence[0].MaskFrameNumbers = 7
    ds.save_as(bad, enforce_file_format=True)
    raw_index = tmp_path / "raw.csv"
    raw_index.write_text(f"study_uid,series_uid,source_path\nstudy,series,{bad}\n", encoding="utf-8")
    effective = tmp_path / "effective.csv"

    with pytest.raises(PresentationBatchError):
        preprocess_series_index(
            raw_index,
            effective,
            source_root=source_root,
            derived_root=tmp_path / "derived",
            audit_csv=tmp_path / "audit.csv",
            summary_json=tmp_path / "summary.json",
        )
    assert not effective.exists()
    assert (tmp_path / "audit.csv").exists()
