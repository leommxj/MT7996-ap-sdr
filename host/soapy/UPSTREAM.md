# Upstream provenance

This directory vendors the SoapySDR module from:

- Repository: <https://github.com/kingjamez/SoapyW1700K>
- Commit: `e8c9df10822fc2f8800ebbdc6033b85be17f4aa6`
- License: MIT, see `LICENSE`

It is copied into this repository so the build machine does not need a second git checkout or a
git submodule.

## Local changes from upstream

- Uses the canonical `host/stream/include/w1700k_stream.h` through `-I../stream/include`; the
  upstream `include/w1700k_stream.h` copy is not vendored.
- Build version is `0.2.0`.
- Module source is otherwise unchanged at the commit above.

## Host-side dependency

The module needs the unit-side lock spans, `rf.lo_ranges_hz`, reported by `w1700k status --rf`.
That change comes from `kingjamez/w1700k-sdr` commit
`06ffa8767ffd4f8416fc1a1ea30f6ea5bb159886` and is applied in this tree in:

- `host/w1700k/controller.py`
- `host/w1700k/README.md`
- `tests/test_host_controller.py`

## Syncing upstream

1. Diff the new upstream tag/commit against `e8c9df1`.
2. Copy `src/`, `tests/`, `LICENSE`, `Makefile`, and `CMakeLists.txt`.
3. Keep the canonical-header change above.
4. Bump the commit in this file and `VERSION` in `Makefile`/`CMakeLists.txt`.
5. Build and run the tests that do not need a unit:
   `make -C host/soapy` and `make check-soapy`.
6. Run the real-device smoke test after reconnecting the 10G host.
