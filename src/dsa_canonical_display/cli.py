from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pydicom

from .core import (
    PresentationBatchError,
    PresentationError,
    classify_presentation,
    derive_presentation_dicom,
    preprocess_series_index,
    presentation_profile_hash,
    presentation_profile_payload,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _nonnegative_float(value: str) -> float:
    import math
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Raw DSA to canonical 12-bit display DICOM")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("profile", help="print fixed numerical profile and hash")
    inspect = sub.add_parser("inspect", help="classify one file without decoding pixels")
    inspect.add_argument("source", type=Path)
    convert = sub.add_parser("convert", help="convert one supported LOG/SUB file")
    convert.add_argument("source", type=Path)
    convert.add_argument("output", type=Path)
    batch = sub.add_parser("batch", help="process a CSV index and publish it only on success")
    for name in ("raw-index", "effective-index", "source-root", "derived-root", "audit-csv", "summary-json"):
        batch.add_argument(f"--{name}", type=Path, required=True)
    batch.add_argument("--reserve-free-gib", type=_nonnegative_float, default=0.0)
    batch.add_argument("--workers", type=_positive_int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "profile":
            result = {"profile": presentation_profile_payload(), "profile_hash": presentation_profile_hash()}
        elif args.command == "inspect":
            header = pydicom.dcmread(args.source, stop_before_pixels=True, force=True)
            result = asdict(classify_presentation(header))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if result["action"] == "UNSUPPORTED" else 0
        elif args.command == "convert":
            output = args.output.resolve()
            if output.exists() and not output.with_suffix(output.suffix + ".presentation.json").is_file():
                raise PresentationError("existing output has no resume sidecar; choose a new output path")
            result = asdict(derive_presentation_dicom(args.source, args.output))
        else:
            result = preprocess_series_index(
                args.raw_index, args.effective_index,
                source_root=args.source_root, derived_root=args.derived_root,
                audit_csv=args.audit_csv, summary_json=args.summary_json,
                reserve_free_bytes=int(args.reserve_free_gib * 1024 ** 3), workers=args.workers,
            )
    except PresentationBatchError as exc:
        print(json.dumps({**exc.summary, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (PresentationError, OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print(json.dumps({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
