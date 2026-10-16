# Local validation — 2026-10-16

Environment: Windows, Python 3.12.7, NumPy 1.26.4, pydicom 3.0.1,
pytest 9.0.3. Tests use synthetic DICOMs only.

- 26 tests passed; 1 POSIX case-sensitivity test skipped on Windows.
- Multiframe subtraction, mean-mask behavior, RLE round trip and timing checked.
- Noise suppression and strong-edge retention checked on synthetic arrays.
- CLI success/failure, source protection, resume revalidation, index publication
  failure and batch preflight guards checked.
- A generated 6-frame example completed the batch CLI with two workers and
  returned READY.
- AST comparison with the original implementation confirmed identical bodies
  for classification, profile payload/hash, spatial filter, tone mapping,
  derived header and deterministic UID functions.
- Source distribution and wheel built successfully; the wheel was installed
  and checked from outside the source directory.

The default local pytest temporary directory had an access restriction; tests
were run with an explicit writable `--basetemp` outside the repository.
GitHub Actions is configured for Linux/Windows and Python 3.11–3.13; this record
does not claim those hosted jobs have already passed.

No real patient data, clinical performance or downstream model accuracy was
re-evaluated in this extraction.
