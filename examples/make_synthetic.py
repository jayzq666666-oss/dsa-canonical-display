"""Generate a small, deterministic demonstration with no patient data."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, XRayAngiographicImageStorage, generate_uid


def make_demo(output: Path) -> Path:
    source = output / "source"
    source.mkdir(parents=True, exist_ok=True)
    path = source / "synthetic-raw.dcm"
    index = output / "raw-index.csv"
    if path.exists() or index.exists():
        raise FileExistsError("demo already exists; choose a new output directory")
    rng = np.random.default_rng(42)
    pixels = np.full((6, 64, 64), 2000.0, dtype=np.float32)
    pixels += rng.normal(0, 4, pixels.shape)
    for frame in range(1, 6):
        pixels[frame, 12:52, 29:33] -= 25 * frame
        pixels[frame, 30:33, 12:52] -= 12 * frame
    pixels = np.rint(np.clip(pixels, 0, 4095)).astype("<u2")
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = XRayAngiographicImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientName = "SYNTHETIC^DEMO"
    ds.PatientID = "SYNTHETIC"
    ds.Modality = "XA"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "SINGLE PLANE"]
    ds.Rows = ds.Columns = 64
    ds.NumberOfFrames = 6
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    ds.PixelIntensityRelationship = "LOG"
    ds.RecommendedViewingMode = "SUB"
    mask = Dataset()
    mask.MaskOperation = "AVG_SUB"
    mask.MaskFrameNumbers = 1
    ds.MaskSubtractionSequence = Sequence([mask])
    ds.FrameTime = 66.7
    ds.PixelData = pixels.tobytes()
    ds.save_as(path, enforce_file_format=True)
    with index.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["study_uid", "series_uid", "source_path"])
        writer.writeheader()
        writer.writerow({"study_uid": ds.StudyInstanceUID, "series_uid": ds.SeriesInstanceUID,
                         "source_path": "source/synthetic-raw.dcm"})
    return index


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("demo"))
    print(make_demo(parser.parse_args().output))
