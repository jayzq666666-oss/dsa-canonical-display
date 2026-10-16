# Source provenance

Extracted from the existing `canonical_display_v2` implementation on 2026-10-16.
Only algorithm code and synthetic tests were imported. No real DICOM files,
patient indexes, screenshots, model credentials, or runtime audit logs are included.

The subtraction, spatial filter, tone curve, profile payload/hash, DICOM output
metadata policy and deterministic UID scheme are retained. Packaging changes
replace the platform hash helper, add a public API and CLI, and remove an unused
histogram helper. Additional guards protect input paths, reject colliding output
names and empty input indexes, resolve relative paths against the CSV directory,
and preserve case-sensitive path identity on POSIX. The summary is marked
PROCESSING after preflight and READY only after publishing the effective index.

Original source SHA-256 (before extraction):

- `backend/services/dsa_presentation.py`: `11257bb40c685de14d5281b77bd4412da2dad83b0ee98963e7f76b589abb4465`
- `tests/test_dsa_presentation.py`: `351028f35edf9c3950f37b74788810c7cb7aabe2342906eb09891fe531649cc3`

This package does not include the web application, model inference services or review database. Historical data validation in the source conversation is not independently reproduced by this repository.
