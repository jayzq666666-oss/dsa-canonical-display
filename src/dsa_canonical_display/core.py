from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pydicom
from pydicom.dataset import Dataset
from pydicom.encaps import encapsulate_buffer
from pydicom.pixels import iter_pixels
from pydicom.pixels.encoders import RLELosslessEncoder
from pydicom.sequence import Sequence
from pydicom.uid import RLELossless, generate_uid

from ._hashing import file_sha256


PROFILE_ID = "canonical_display_v2"
OUTPUT_BITS = 12
OUTPUT_MAX = (1 << OUTPUT_BITS) - 1
NEUTRAL_VALUE = 3013
NEGATIVE_GAIN = 5.5
POSITIVE_GAIN = 4.0
NOISE_SIGMA_LOW = 0.75
NOISE_SIGMA_HIGH = 3.0
MAX_MASK_FRAMES = 8


class PresentationError(RuntimeError):
    pass


class PresentationBatchError(PresentationError):
    def __init__(self, message: str, *, summary: dict[str, Any]):
        super().__init__(message)
        self.summary = summary


@dataclass(frozen=True)
class PresentationDecision:
    action: str
    reason: str
    mask_frames: tuple[int, ...] = ()


@dataclass(frozen=True)
class PresentationResult:
    source_path: str
    effective_path: str
    status: str
    reason: str
    profile_id: str
    mask_frames: str = ""
    low_delta: float | None = None
    high_delta: float | None = None
    source_series_uid: str = ""
    derived_series_uid: str = ""
    source_sop_uid: str = ""
    derived_sop_uid: str = ""
    source_artifact_sha256: str = ""
    artifact_sha256: str = ""
    pixel_semantic_sha256: str = ""
    rows: int = 0
    columns: int = 0
    number_of_frames: int = 0
    error: str = ""


def presentation_profile_payload() -> dict[str, Any]:
    return {
        "profile_id": PROFILE_ID,
        "operation": "frame-minus-mean-mask",
        "mask_frame_index_base": 1,
        "tone_curve": "asymmetric-tanh-soft-knee",
        "negative_gain": NEGATIVE_GAIN,
        "positive_gain": POSITIVE_GAIN,
        "spatial_filter": "3x3-binomial-noise-adaptive-edge-preserving",
        "noise_sigma_low": NOISE_SIGMA_LOW,
        "noise_sigma_high": NOISE_SIGMA_HIGH,
        "neutral_value": NEUTRAL_VALUE,
        "output_min": 0,
        "output_max": OUTPUT_MAX,
        "bits_stored": OUTPUT_BITS,
        "photometric_interpretation": "MONOCHROME2",
        "pixel_intensity_relationship": "DISP",
        "transfer_syntax_uid": str(RLELossless),
    }


def presentation_profile_hash() -> str:
    payload = json.dumps(presentation_profile_payload(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_presentation(dataset: Dataset) -> PresentationDecision:
    relationship = _upper(dataset.get("PixelIntensityRelationship", ""))
    viewing_mode = _upper(dataset.get("RecommendedViewingMode", ""))
    mask_sequence = dataset.get("MaskSubtractionSequence")

    if relationship == "DISP":
        return PresentationDecision("PASSTHROUGH", "already-display-domain")
    if viewing_mode != "SUB":
        if viewing_mode == "NAT":
            return PresentationDecision("PASSTHROUGH", "natural-viewing-mode")
        if relationship == "LIN":
            return PresentationDecision("PASSTHROUGH", "linear-non-subtracted")
        return PresentationDecision("PASSTHROUGH", "no-subtraction-requested")
    if relationship != "LOG":
        return PresentationDecision("UNSUPPORTED", f"SUB requires LOG input, got {relationship or 'missing'}")
    if not mask_sequence or len(mask_sequence) != 1:
        return PresentationDecision("UNSUPPORTED", "SUB requires exactly one MaskSubtractionSequence item")

    item = mask_sequence[0]
    operation = _upper(item.get("MaskOperation", ""))
    if operation != "AVG_SUB":
        return PresentationDecision("UNSUPPORTED", f"unsupported MaskOperation={operation or 'missing'}")
    unsupported = [
        name
        for name in (
            "ApplicableFrameRange",
            "ContrastFrameAveraging",
            "MaskSubPixelShift",
            "MaskVisibilityPercentage",
        )
        if item.get(name) not in (None, "")
    ]
    if unsupported:
        return PresentationDecision("UNSUPPORTED", f"unsupported mask attributes: {','.join(unsupported)}")

    raw_frames = item.get("MaskFrameNumbers")
    if raw_frames in (None, ""):
        return PresentationDecision("UNSUPPORTED", "MaskFrameNumbers is missing")
    if isinstance(raw_frames, (str, bytes)) or not isinstance(raw_frames, Iterable):
        raw_frames = [raw_frames]
    try:
        mask_frames = tuple(int(value) for value in raw_frames)
    except (TypeError, ValueError):
        return PresentationDecision("UNSUPPORTED", "MaskFrameNumbers is invalid")
    frame_count = int(dataset.get("NumberOfFrames", 1) or 1)
    if not mask_frames or len(mask_frames) > MAX_MASK_FRAMES:
        return PresentationDecision("UNSUPPORTED", f"mask frame count must be 1..{MAX_MASK_FRAMES}")
    if len(set(mask_frames)) != len(mask_frames) or any(value < 1 or value > frame_count for value in mask_frames):
        return PresentationDecision("UNSUPPORTED", "MaskFrameNumbers contains duplicates or out-of-range values")
    if int(dataset.get("SamplesPerPixel", 1) or 1) != 1:
        return PresentationDecision("UNSUPPORTED", "only single-sample grayscale DICOM is supported")
    if _upper(dataset.get("PhotometricInterpretation", "")) not in {"MONOCHROME1", "MONOCHROME2"}:
        return PresentationDecision("UNSUPPORTED", "only monochrome DICOM is supported")
    return PresentationDecision("DERIVE", "log-avg-sub", mask_frames)


def derive_presentation_dicom(source: str | Path, output: str | Path) -> PresentationResult:
    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    if source_path == output_path or (
        output_path.exists() and os.path.samefile(source_path, output_path)
    ):
        raise PresentationError("source and output must be different files")
    header = pydicom.dcmread(str(source_path), stop_before_pixels=True, force=True)
    decision = classify_presentation(header)
    if decision.action != "DERIVE":
        raise PresentationError(f"DICOM is not derivable: {decision.action}: {decision.reason}")

    source_sha = file_sha256(source_path)
    resume = _load_resume_result(source_path, output_path, source_sha)
    if resume is not None:
        return resume

    frame_count = int(header.get("NumberOfFrames", 1) or 1)
    rows = int(header.Rows)
    columns = int(header.Columns)
    mask_indices = [value - 1 for value in decision.mask_frames]
    mask_sum: np.ndarray | None = None
    for frame in iter_pixels(str(source_path), indices=mask_indices, raw=True):
        current = np.asarray(frame, dtype=np.int64)
        mask_sum = current.copy() if mask_sum is None else mask_sum + current
    if mask_sum is None or mask_sum.shape != (rows, columns):
        raise PresentationError("unable to decode the configured mask frames")

    mask_count = len(decision.mask_frames)
    mask_mean = mask_sum.astype(np.float32) / mask_count

    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_name(f".{output_path.name}.partial")
    semantic = hashlib.sha256()
    semantic.update(json.dumps({
        "profile": presentation_profile_payload(),
        "shape": [frame_count, rows, columns],
        "dtype": "uint16-le",
        "frame_time": _json_value(header.get("FrameTimeVector", header.get("FrameTime", ""))),
        "cine_rate": _json_value(header.get("CineRate", "")),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    source_series_uid = str(header.get("SeriesInstanceUID", ""))
    source_sop_uid = str(header.get("SOPInstanceUID", ""))
    derived_series_uid = _derived_uid("series", source_series_uid)
    derived_sop_uid = _derived_uid("sop", source_sop_uid or source_sha)

    with tempfile.TemporaryDirectory(prefix=f".{output_path.stem}.rle-", dir=output_path.parent) as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        frame_paths: list[Path] = []
        for index, frame in enumerate(iter_pixels(str(source_path), raw=True)):
            delta = np.asarray(frame, dtype=np.float32) - mask_mean
            filtered_delta = _edge_preserving_noise_filter(delta)
            mapped = _map_to_display(filtered_delta)
            semantic.update(mapped.astype("<u2", copy=False).tobytes(order="C"))
            encoded = RLELosslessEncoder.encode(
                mapped,
                rows=rows,
                columns=columns,
                samples_per_pixel=1,
                bits_allocated=16,
                bits_stored=OUTPUT_BITS,
                number_of_frames=1,
                pixel_representation=0,
                photometric_interpretation="MONOCHROME2",
            )
            frame_path = temp_dir / f"{index:06d}.rle"
            frame_path.write_bytes(encoded)
            frame_paths.append(frame_path)

        handles = [path.open("rb") for path in frame_paths]
        try:
            derived = _derived_header(header, source_series_uid, source_sop_uid, derived_series_uid, derived_sop_uid)
            derived.PixelData = encapsulate_buffer(handles, has_bot=True)
            derived["PixelData"].VR = "OB"
            derived["PixelData"].is_undefined_length = True
            derived.save_as(str(partial_path), enforce_file_format=True)
        finally:
            for handle in handles:
                handle.close()
    os.replace(partial_path, output_path)

    result = PresentationResult(
        source_path=str(source_path),
        effective_path=str(output_path),
        status="DERIVED",
        reason=decision.reason,
        profile_id=PROFILE_ID,
        mask_frames=";".join(str(value) for value in decision.mask_frames),
        low_delta=-NEUTRAL_VALUE / NEGATIVE_GAIN,
        high_delta=(OUTPUT_MAX - NEUTRAL_VALUE) / POSITIVE_GAIN,
        source_series_uid=source_series_uid,
        derived_series_uid=derived_series_uid,
        source_sop_uid=source_sop_uid,
        derived_sop_uid=derived_sop_uid,
        source_artifact_sha256=source_sha,
        artifact_sha256=file_sha256(output_path),
        pixel_semantic_sha256=semantic.hexdigest(),
        rows=rows,
        columns=columns,
        number_of_frames=frame_count,
    )
    _write_resume_result(output_path, result)
    return result


def preprocess_series_index(
    raw_index: str | Path,
    effective_index: str | Path,
    *,
    source_root: str | Path,
    derived_root: str | Path,
    audit_csv: str | Path,
    summary_json: str | Path,
    reserve_free_bytes: int = 0,
    workers: int = 1,
) -> dict[str, Any]:
    raw_index_path = Path(raw_index).resolve()
    effective_index_path = Path(effective_index).resolve()
    source_root_path = Path(source_root).resolve()
    derived_root_path = Path(derived_root).resolve()
    with raw_index_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "source_path" not in fieldnames:
        raise PresentationBatchError("series index is missing source_path", summary={"status": "FAILED"})

    if not rows or any(not str(row.get("source_path", "")).strip() for row in rows):
        raise PresentationBatchError("index must contain nonempty source_path rows", summary={"status": "FAILED"})
    # Relative source paths are anchored to the input CSV, independent of the shell cwd.
    for row in rows:
        paths = []
        for part in str(row["source_path"]).split(";"):
            if part.strip():
                path = Path(part.strip())
                paths.append(str((raw_index_path.parent / path).resolve() if not path.is_absolute() else path.resolve()))
        row["source_path"] = ";".join(paths)
        if not paths:
            raise PresentationBatchError("each row requires at least one source file", summary={"status": "FAILED"})
    _validate_batch_paths(
        rows, raw_index_path, effective_index_path, source_root_path,
        derived_root_path, Path(audit_csv).resolve(), Path(summary_json).resolve(),
    )

    if reserve_free_bytes:
        _assert_capacity(rows, source_root_path, derived_root_path, reserve_free_bytes)

    _write_json_atomic(Path(summary_json), {
        "status": "PROCESSING", "profile_hash": presentation_profile_hash(),
    })

    source_paths: list[Path] = []
    seen_sources: set[str] = set()
    for row in rows:
        for value in (part.strip() for part in str(row.get("source_path", "")).split(";")):
            if not value:
                continue
            source_path = Path(value).resolve()
            cache_key = _path_key(source_path)
            if cache_key not in seen_sources:
                seen_sources.add(cache_key)
                source_paths.append(source_path)

    worker_count = max(1, int(workers))
    if worker_count == 1:
        audit_rows = [_preprocess_one(path, source_root_path, derived_root_path) for path in source_paths]
    else:
        worker = partial(_preprocess_one, source_root=source_root_path, derived_root=derived_root_path)
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            audit_rows = list(executor.map(worker, source_paths))
    results_by_source = {_path_key(result.source_path): result for result in audit_rows}
    failures = [result for result in audit_rows if result.status in {"FAILED", "UNSUPPORTED"}]

    effective_rows: list[dict[str, str]] = []
    for row in rows:
        effective_paths: list[str] = []
        for value in (part.strip() for part in str(row.get("source_path", "")).split(";")):
            if not value:
                continue
            result = results_by_source[_path_key(Path(value))]
            effective_paths.append(result.effective_path if result.status in {"DERIVED", "PASSTHROUGH"} else "")
        updated = dict(row)
        updated["source_path"] = ";".join(path for path in effective_paths if path)
        effective_rows.append(updated)

    counts: dict[str, int] = {}
    for result in audit_rows:
        counts[result.status] = counts.get(result.status, 0) + 1
    summary = {
        "status": "FAILED" if failures else "READY",
        "profile": presentation_profile_payload(),
        "profile_hash": presentation_profile_hash(),
        "source_root": str(source_root_path),
        "derived_root": str(derived_root_path),
        "series_rows": len(rows),
        "files": len(audit_rows),
        "counts": counts,
        "failures": [asdict(item) for item in failures],
    }
    _write_audit(Path(audit_csv), audit_rows)
    if failures:
        _write_json_atomic(Path(summary_json), summary)
        raise PresentationBatchError(f"presentation preprocessing failed for {len(failures)} files", summary=summary)

    effective_index_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", delete=False, dir=effective_index_path.parent) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(effective_rows)
        temporary = Path(handle.name)
    os.replace(temporary, effective_index_path)
    _write_json_atomic(Path(summary_json), summary)
    return summary


def _preprocess_one(source_path: Path, source_root: Path, derived_root: Path) -> PresentationResult:
    try:
        header = pydicom.dcmread(str(source_path), stop_before_pixels=True, force=True)
        decision = classify_presentation(header)
        if decision.action == "DERIVE":
            try:
                relative = source_path.relative_to(source_root)
            except ValueError as exc:
                raise PresentationError("source file is outside source_root") from exc
            output = _derived_output_path(relative, derived_root)
            return derive_presentation_dicom(source_path, output)
        source_sha = file_sha256(source_path)
        return PresentationResult(
            source_path=str(source_path),
            effective_path=str(source_path),
            status=decision.action,
            reason=decision.reason,
            profile_id=PROFILE_ID,
            source_series_uid=str(header.get("SeriesInstanceUID", "")),
            source_sop_uid=str(header.get("SOPInstanceUID", "")),
            source_artifact_sha256=source_sha,
            artifact_sha256=source_sha,
            pixel_semantic_sha256=hashlib.sha256(f"passthrough:{source_sha}".encode("ascii")).hexdigest(),
            rows=int(header.get("Rows", 0) or 0),
            columns=int(header.get("Columns", 0) or 0),
            number_of_frames=int(header.get("NumberOfFrames", 1) or 1),
        )
    except Exception as exc:
        return PresentationResult(
            source_path=str(source_path),
            effective_path="",
            status="FAILED",
            reason="preprocessing-error",
            profile_id=PROFILE_ID,
            error=f"{type(exc).__name__}: {exc}",
        )


def _assert_capacity(rows: list[dict[str, str]], source_root: Path, derived_root: Path, reserve_free_bytes: int) -> None:
    derivable_bytes = 0
    seen: set[str] = set()
    for row in rows:
        for value in (part.strip() for part in str(row.get("source_path", "")).split(";")):
            if not value:
                continue
            source = Path(value).resolve()
            key = _path_key(source)
            if key in seen:
                continue
            seen.add(key)
            try:
                header = pydicom.dcmread(str(source), stop_before_pixels=True, force=True)
                if classify_presentation(header).action == "DERIVE":
                    relative = source.relative_to(source_root)
                    output = _derived_output_path(relative, derived_root)
                    if not output.is_file() or not _resume_path(output).is_file():
                        derivable_bytes += source.stat().st_size
            except (OSError, ValueError):
                continue
    ancestor = derived_root
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    free_bytes = shutil.disk_usage(ancestor).free
    estimated_output = math.ceil(derivable_bytes * 0.62)
    required = estimated_output + reserve_free_bytes
    if free_bytes < required:
        raise PresentationBatchError(
            "insufficient free space for canonical display artifacts",
            summary={
                "status": "FAILED",
                "reason": "INSUFFICIENT_STORAGE",
                "free_bytes": free_bytes,
                "estimated_output_bytes": estimated_output,
                "reserve_bytes": reserve_free_bytes,
                "required_bytes": required,
                "derived_root": str(derived_root),
            },
        )


def _derived_output_path(relative_source: Path, derived_root: Path) -> Path:
    return derived_root / relative_source.parent / f"{relative_source.stem}.display-v2.dcm"


def _edge_preserving_noise_filter(delta: np.ndarray) -> np.ndarray:
    """Suppress small high-frequency residuals while retaining vessel edges."""
    values = np.asarray(delta, dtype=np.float32)
    padded = np.pad(values, 1, mode="edge")
    smooth = (
        padded[:-2, :-2] + 2 * padded[:-2, 1:-1] + padded[:-2, 2:]
        + 2 * padded[1:-1, :-2] + 4 * padded[1:-1, 1:-1] + 2 * padded[1:-1, 2:]
        + padded[2:, :-2] + 2 * padded[2:, 1:-1] + padded[2:, 2:]
    ) / 16.0
    residual = values - smooth
    rows, columns = values.shape
    roi = residual[
        rows // 5 : max(rows // 5 + 1, 4 * rows // 5),
        columns // 5 : max(columns // 5 + 1, 4 * columns // 5),
    ]
    median = float(np.median(roi))
    sigma = max(0.5, 1.4826 * float(np.median(np.abs(roi - median))))
    magnitude = np.abs(residual)
    low = NOISE_SIGMA_LOW * sigma
    high = NOISE_SIGMA_HIGH * sigma
    weight = np.clip((magnitude - low) / max(high - low, 1e-6), 0.0, 1.0)
    weight = weight * weight * (3.0 - 2.0 * weight)
    return smooth + residual * weight


def _map_to_display(delta: np.ndarray) -> np.ndarray:
    values = np.asarray(delta, dtype=np.float32)
    mapped = np.empty(values.shape, dtype=np.float32)
    negative = values <= 0
    mapped[negative] = NEUTRAL_VALUE - NEUTRAL_VALUE * np.tanh(
        (-values[negative] * NEGATIVE_GAIN) / NEUTRAL_VALUE
    )
    positive_range = OUTPUT_MAX - NEUTRAL_VALUE
    mapped[~negative] = NEUTRAL_VALUE + positive_range * np.tanh(
        (values[~negative] * POSITIVE_GAIN) / positive_range
    )
    return np.rint(np.clip(mapped, 0, OUTPUT_MAX)).astype(np.uint16)


def _derived_header(
    source: Dataset,
    source_series_uid: str,
    source_sop_uid: str,
    derived_series_uid: str,
    derived_sop_uid: str,
) -> Dataset:
    derived = source.copy()
    derived.file_meta = source.file_meta.copy()
    derived.file_meta.TransferSyntaxUID = RLELossless
    derived.file_meta.MediaStorageSOPInstanceUID = derived_sop_uid
    derived.SeriesInstanceUID = derived_series_uid
    derived.SOPInstanceUID = derived_sop_uid
    image_type = [str(value) for value in derived.get("ImageType", [])]
    preserved = [value for value in image_type[2:] if value not in {"DERIVED", "PRIMARY"}]
    derived.ImageType = ["DERIVED", "PRIMARY", *preserved]
    derived.DerivationDescription = (
        f"{PROFILE_ID}: AVG_SUB mask subtraction; noise-adaptive edge-preserving filter; "
        f"asymmetric tanh mapping; neutral={NEUTRAL_VALUE}; 12-bit RLE Lossless"
    )
    if source_sop_uid:
        reference = Dataset()
        reference.ReferencedSOPClassUID = str(source.get("SOPClassUID", ""))
        reference.ReferencedSOPInstanceUID = source_sop_uid
        derived.SourceImageSequence = Sequence([reference])
    derived.SamplesPerPixel = 1
    derived.PhotometricInterpretation = "MONOCHROME2"
    derived.BitsAllocated = 16
    derived.BitsStored = OUTPUT_BITS
    derived.HighBit = OUTPUT_BITS - 1
    derived.PixelRepresentation = 0
    derived.PixelIntensityRelationship = "DISP"
    derived.RescaleSlope = 1
    derived.RescaleIntercept = 0
    derived.WindowCenter = 2047
    derived.WindowWidth = 4095
    derived.VOILUTFunction = "LINEAR"
    derived.LossyImageCompression = "00"
    for keyword in (
        "MaskSubtractionSequence",
        "RecommendedViewingMode",
        "VOILUTSequence",
        "PixelPaddingValue",
        "PixelPaddingRangeLimit",
        "SmallestImagePixelValue",
        "LargestImagePixelValue",
    ):
        if keyword in derived:
            del derived[keyword]
    return derived


def _derived_uid(kind: str, source_uid: str) -> str:
    return generate_uid(entropy_srcs=[PROFILE_ID, kind, source_uid])


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _json_value(value: Any) -> Any:
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
        return [str(item) for item in value]
    return str(value or "")


def _resume_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".presentation.json")


def _load_resume_result(source: Path, output: Path, source_sha: str) -> PresentationResult | None:
    metadata_path = _resume_path(output)
    if not output.is_file() or not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if payload.get("profile_hash") != presentation_profile_hash():
            return None
        result = PresentationResult(**payload["result"])
        if (result.source_path != str(source) or result.effective_path != str(output)
                or result.source_artifact_sha256 != source_sha):
            return None
        if file_sha256(output) != result.artifact_sha256:
            return None
        return result
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _write_resume_result(output: Path, result: PresentationResult) -> None:
    payload = {"profile_hash": presentation_profile_hash(), "result": asdict(result)}
    _write_json_atomic(_resume_path(output), payload)


def _write_audit(path: Path, rows: list[PresentationResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(PresentationResult.__dataclass_fields__)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", delete=False, dir=path.parent) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _validate_batch_paths(
    rows: list[dict[str, str]], raw_index: Path, effective_index: Path,
    source_root: Path, derived_root: Path, audit_csv: Path, summary_json: Path,
) -> None:
    def fail(message: str) -> None:
        raise PresentationBatchError(message, summary={"status": "FAILED", "reason": message})

    if not source_root.is_dir():
        fail("source_root must be an existing directory")
    if (derived_root.is_relative_to(source_root) or source_root.is_relative_to(derived_root)):
        fail("source_root and derived_root must not overlap")
    reports = [effective_index, audit_csv, summary_json]
    if len({_path_key(p) for p in [raw_index, *reports]}) != 4:
        fail("input index and report paths must all differ")
    for path in reports:
        if path.is_relative_to(source_root):
            fail("report paths must be outside source_root")
    reserved = {_path_key(p) for p in [raw_index, *reports]}
    owners: dict[str, str] = {}
    for row in rows:
        for value in row["source_path"].split(";"):
            source = Path(value).resolve()
            if not source.is_relative_to(source_root):
                fail("every source file must be inside source_root")
            source_key = _path_key(source)
            if source_key in reserved:
                fail("source files must differ from indexes and reports")
            output = _derived_output_path(source.relative_to(source_root), derived_root).resolve()
            if not output.is_relative_to(derived_root):
                fail("derived file resolves outside derived_root")
            for artifact in (output, _resume_path(output)):
                key = _path_key(artifact)
                if key in reserved:
                    fail("derived artifacts must differ from indexes and reports")
                if key in owners and owners[key] != source_key:
                    fail("source filenames map to the same derived artifact")
                owners[key] = source_key
