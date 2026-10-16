from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pydicom
import pytest

from dsa_canonical_display import (
    PresentationBatchError, PresentationError, derive_presentation_dicom,
    preprocess_series_index,
)
from test_core import _write_dicom


def _source(root: Path, name: str = "raw.dcm") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    _write_dicom(path, np.stack([np.full((8, 8), v, dtype=np.uint16) for v in (2000, 1900, 1800)]))
    return path


def _index(tmp: Path, paths: list[Path]) -> Path:
    path = tmp / "raw.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_path", "custom"])
        writer.writeheader()
        writer.writerow({"source_path": ";".join(map(str, paths)), "custom": "preserved"})
    return path


def _batch(tmp: Path, source: Path, index: Path, **overrides):
    kwargs = dict(source_root=source, derived_root=tmp / "derived", audit_csv=tmp / "audit.csv",
                  summary_json=tmp / "summary.json")
    kwargs.update(overrides)
    return preprocess_series_index(index, tmp / "effective.csv", **kwargs)


def test_direct_conversion_cannot_overwrite_source(tmp_path):
    source = _source(tmp_path / "source")
    before = source.read_bytes()
    with pytest.raises(PresentationError, match="different files"):
        derive_presentation_dicom(source, source)
    assert source.read_bytes() == before


def test_mean_of_multiple_mask_frames_and_timing(tmp_path):
    source = _source(tmp_path / "source")
    ds = pydicom.dcmread(source)
    ds.MaskSubtractionSequence[0].MaskFrameNumbers = [1, 3]
    ds.FrameTimeVector = [0, 30, 70]
    del ds.FrameTime
    ds.save_as(source, enforce_file_format=True)
    output = tmp_path / "out.dcm"
    result = derive_presentation_dicom(source, output)
    converted = pydicom.dcmread(output)
    assert result.mask_frames == "1;3"
    assert converted.pixel_array.shape == (3, 8, 8)
    assert np.all(converted.pixel_array[1] == 3013)
    assert np.all(converted.pixel_array[0] > 3013)
    assert np.all(converted.pixel_array[2] < 3013)
    assert list(converted.FrameTimeVector) == list(ds.FrameTimeVector)


@pytest.mark.parametrize("damage", ["source", "output", "sidecar", "profile"])
def test_resume_revalidates_content(tmp_path, damage):
    source = _source(tmp_path / "source")
    output = tmp_path / "out.dcm"
    first = derive_presentation_dicom(source, output)
    if damage == "source":
        ds = pydicom.dcmread(source)
        ds.FrameTime = 100
        ds.save_as(source, enforce_file_format=True)
    elif damage == "output":
        output.write_bytes(b"corrupted")
    else:
        sidecar = output.with_suffix(".dcm.presentation.json")
        payload = json.loads(sidecar.read_text())
        if damage == "profile":
            payload["profile_hash"] = "invalid"
        else:
            payload["result"]["effective_path"] = "wrong/path"
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
    second = derive_presentation_dicom(source, output)
    assert pydicom.dcmread(output).pixel_array.shape == (3, 8, 8)
    if damage == "source":
        assert first.source_artifact_sha256 != second.source_artifact_sha256
    else:
        assert first.artifact_sha256 == second.artifact_sha256


def test_batch_relative_paths_and_duplicate_sources(tmp_path, monkeypatch):
    source = _source(tmp_path / "source")
    index = _index(tmp_path, [Path("source/raw.dcm"), Path("source/raw.dcm")])
    elsewhere = tmp_path / "unrelated-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    summary = _batch(tmp_path, source.parent, index)
    assert summary["files"] == 1
    with (tmp_path / "effective.csv").open(encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle))
    assert row["custom"] == "preserved"
    assert len(row["source_path"].split(";")) == 2


@pytest.mark.parametrize("kind", ["same-root", "nested-derived", "nested-source", "report-alias", "report-in-source", "outside-source", "stem-collision", "empty"])
def test_preflight_rejects_destructive_or_ambiguous_paths(tmp_path, kind):
    source = _source(tmp_path / "source")
    index = _index(tmp_path, [source])
    kwargs = {}
    if kind == "same-root":
        kwargs["derived_root"] = source.parent
    elif kind == "nested-derived":
        kwargs["derived_root"] = source.parent / "derived"
    elif kind == "nested-source":
        kwargs["derived_root"] = tmp_path
    elif kind == "report-alias":
        kwargs["audit_csv"] = index
    elif kind == "report-in-source":
        kwargs["summary_json"] = source.parent / "summary.json"
    elif kind == "outside-source":
        index = _index(tmp_path, [_source(tmp_path / "outside")])
    elif kind == "stem-collision":
        index = _index(tmp_path, [source, _source(source.parent, "raw.IMA")])
    else:
        index.write_text("source_path\n", encoding="utf-8")
    before = source.read_bytes()
    with pytest.raises(PresentationBatchError):
        _batch(tmp_path, source.parent, index, **kwargs)
    assert source.read_bytes() == before
    assert not (tmp_path / "effective.csv").exists()


def test_failed_rerun_preserves_existing_effective_index(tmp_path):
    source = _source(tmp_path / "source")
    index = _index(tmp_path, [source])
    _batch(tmp_path, source.parent, index)
    effective = tmp_path / "effective.csv"
    before = effective.read_bytes()
    ds = pydicom.dcmread(source)
    ds.MaskSubtractionSequence[0].MaskFrameNumbers = 99
    ds.save_as(source, enforce_file_format=True)
    with pytest.raises(PresentationBatchError):
        _batch(tmp_path, source.parent, index)
    assert effective.read_bytes() == before
    assert json.loads((tmp_path / "summary.json").read_text())["status"] == "FAILED"


@pytest.mark.skipif(os.name == "nt", reason="Windows filesystem is case insensitive")
def test_distinct_posix_case_is_not_deduplicated(tmp_path):
    lower = _source(tmp_path / "source", "a.dcm")
    upper = _source(lower.parent, "A.dcm")
    summary = _batch(tmp_path, lower.parent, _index(tmp_path, [lower, upper]))
    assert summary["counts"] == {"DERIVED": 2}


def test_cli_success_failure_and_existing_output(tmp_path):
    source = _source(tmp_path / "source")
    command = [sys.executable, "-m", "dsa_canonical_display"]
    inspected = subprocess.run([*command, "inspect", str(source)], capture_output=True, text=True)
    assert inspected.returncode == 0
    assert json.loads(inspected.stdout)["action"] == "DERIVE"
    output = tmp_path / "derived.dcm"
    converted = subprocess.run([*command, "convert", str(source), str(output)], capture_output=True, text=True)
    assert converted.returncode == 0, converted.stderr
    assert json.loads(converted.stdout)["status"] == "DERIVED"
    missing = subprocess.run([*command, "inspect", str(tmp_path / "missing.dcm")], capture_output=True, text=True)
    assert missing.returncode == 2
    assert json.loads(missing.stderr)["status"] == "FAILED"
    before = source.read_bytes()
    protected = subprocess.run([*command, "convert", str(source), str(source)], capture_output=True, text=True)
    assert protected.returncode == 2
    assert source.read_bytes() == before


def test_source_is_not_changed_by_conversion(tmp_path):
    source = _source(tmp_path / "source")
    before = source.read_bytes()
    derive_presentation_dicom(source, tmp_path / "derived.dcm")
    assert source.read_bytes() == before


def test_separator_only_row_is_not_ready(tmp_path):
    source = _source(tmp_path / "source")
    index = tmp_path / "raw.csv"
    index.write_text('source_path\n"; ;"\n', encoding="utf-8")
    with pytest.raises(PresentationBatchError):
        _batch(tmp_path, source.parent, index)


def test_ready_summary_is_not_written_before_index(tmp_path, monkeypatch):
    import dsa_canonical_display.core as core
    source = _source(tmp_path / "source")
    index = _index(tmp_path, [source])
    replace = core.os.replace

    def fail_index(src, dst):
        if Path(dst) == tmp_path / "effective.csv":
            raise OSError("simulated index publication error")
        return replace(src, dst)

    monkeypatch.setattr(core.os, "replace", fail_index)
    with pytest.raises(OSError, match="publication error"):
        _batch(tmp_path, source.parent, index)
    assert not (tmp_path / "effective.csv").exists()
    assert json.loads((tmp_path / "summary.json").read_text())["status"] == "PROCESSING"
