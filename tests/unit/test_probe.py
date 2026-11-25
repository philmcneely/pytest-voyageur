"""
===============================================================================
Vendored Probe (the page script the client runs is the service's, byte for byte)
===============================================================================
The probe is the one piece of code that reads a page, so it is vendored with its sha256 recorded, and the service's own copy
is compared with it. A newer service that changed the probe fails the last test, which is the prompt to copy it again with
`scripts/sync_probe.py` and read the diff.

Features:
    ✓ probe.sha256 names probe.js and its sha256 equals the file's
    ✓ The vendored probe never reads a typed value (the lines of the service's own promise are still there)
    ✓ The service's GET /v1/probe.js equals the vendored file (needs the `voyageur` dev dependency)

Environment Variables:
    None.

Author: PMAC
===============================================================================
"""
import hashlib
from pathlib import Path

import pytest

import pytest_voyageur

PACKAGE = Path(pytest_voyageur.__file__).parent


def test_the_recorded_sha256_is_the_files():
    recorded = (PACKAGE / "probe.sha256").read_text().split()
    assert recorded[1] == "probe.js" and recorded[0] == hashlib.sha256((PACKAGE / "probe.js").read_bytes()).hexdigest()


def test_the_probe_is_a_function_that_takes_the_one_element_or_nothing():
    text = (PACKAGE / "probe.js").read_text(encoding="utf-8")
    assert "(only) => {" in text and "never what a person typed" in text


def test_the_service_serves_the_same_probe():
    pytest.importorskip("voyageur", reason="the voyageur service is not installed (pip install -e .[dev])")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from tests.support import ScriptedModel, build_app
    served = TestClient(build_app(ScriptedModel([])), base_url="http://127.0.0.1").get("/v1/probe.js").content
    assert hashlib.sha256(served).hexdigest() == (PACKAGE / "probe.sha256").read_text().split()[0]
    assert served == (PACKAGE / "probe.js").read_bytes()
