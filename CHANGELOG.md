# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added
- Iframe heals: the locator validators accept `frame_locator('<literal selector>')` as the root call and as a hop anywhere in a
  chain (nested frames included), so a heal the service writes inside an iframe is applied and kept. A chain may not end on a
  frame (it names no element); non-literal arguments and unknown methods are refused as before. `content_frame` is not accepted:
  the service never writes it. End-to-end test with a same-origin payment iframe whose field drifts.
- README: install from GitHub, quick start, how the client talks to the service, troubleshooting, and real output captured from
  a run (`docs/images/`).
- `SECURITY.md`, `CONTRIBUTING.md` (contributions come in under MIT, no CLA), and this file.
- `tests/unit/test_source_hygiene.py`: the repository names no host, LAN address, key, local path, personal address or internal
  product.

### Changed
- The recorder stores the TOP page's path for an element inside an iframe (the probe's own path there is the frame document's, a
  route with no iframe in it, so a heal that had to reopen the page found nothing).
- The `summary:` line prints the path relative to pytest's rootdir when it is inside it, absolute only outside.
- Pinned to Voyageur service commit `277f319` (probe re-synced, byte-identical to the service's copy).

## 0.1.0

First version: the pytest plugin client for a Voyageur service. `pytest --heal` probes the page a test failed on, asks the
service for a locator, checks it in the live page, applies it under strict client-side safety checks, re-runs the exact test
and keeps the edit only if the service confirms it. Includes `--heal-record` fingerprints, the `voyageur` fixture
(`audit`, `heal`) and language branches (`by_language`). Licensed MIT.
