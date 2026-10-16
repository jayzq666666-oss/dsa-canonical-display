# DSA Canonical Display

A standalone Python package extracted from the existing `canonical_display_v2`
pipeline. It converts supported raw LOG/SUB multiframe DSA into 12-bit DISP
DICOMs using mean-mask subtraction, a spatial noise-adaptive filter and an
asymmetric tanh display curve. No model server or web application is required.

Requires Python 3.11+, NumPy and pydicom. Install and run from the repository root:

```bash
python -m pip install -e ".[dev]"
python examples/make_synthetic.py --output demo
dsa-display inspect demo/source/synthetic-raw.dcm
dsa-display convert demo/source/synthetic-raw.dcm demo/single.display-v2.dcm
dsa-display batch --raw-index demo/raw-index.csv --effective-index demo/effective.csv --source-root demo/source --derived-root demo/derived --audit-csv demo/audit.csv --summary-json demo/summary.json --workers 2
python -m pytest
```

CSV input requires `source_path`; other columns are preserved. Semicolons separate
multiple source files per row. Relative paths are resolved from the input CSV
directory. All sources must lie within `--source-root`; the derived root must
not overlap it. Reports must be outside the source root, with distinct paths.

Supported subtraction inputs require LOG, SUB, one AVG_SUB mask sequence and
1–8 unique valid one-based mask frames. Frame ranges, contrast averaging,
subpixel shifts and mask visibility settings are rejected. Existing DISP and
inputs without a SUB request pass through without copying. Pass-through is
not a downstream-model compatibility check. Compressed source files may need
additional decoder libraries for their transfer syntax.

The fixed profile uses neutral=3013, negative gain=5.5 and positive gain=4.0.
There is no per-sequence min/max stretching, temporal smoothing or motion
registration. Output is MONOCHROME2, unsigned 12-bit in a 16-bit container,
RLE Lossless, with new Series/SOP UIDs and preserved study/frame timing.

Batch processing writes a per-file audit CSV, summary JSON and resume sidecars.
Only a successful batch replaces the effective index. A previously published
index remains after failure: consumers must check the current summary is READY.
Preflight failures return an error without writing new reports. Re-running the
same command validates profile/source/output hashes before reusing artifacts.
Use dedicated output paths and one writer per output directory.

The Python API exports `classify_presentation`, `derive_presentation_dicom`,
`preprocess_series_index`, profile metadata and typed results/errors. The direct
conversion API can regenerate an existing output when its resume cache is invalid;
the CLI refuses an existing output without a sidecar. Exit code 0 means success;
2 means unsupported input, invalid arguments or failure.

This is research preprocessing code with calibration inherited from one data
domain. Validate image quality and model behavior when moving to a new acquisition
domain. This repository contains only synthetic examples. Conversion preserves
patient/private metadata, source UIDs and possible burned-in text: it does not
de-identify DICOMs. Keep generated files and runtime reports out of public repos.

[Algorithm](ALGORITHM.md) · [Provenance](PROVENANCE.md) · [MIT License](../LICENSE)
