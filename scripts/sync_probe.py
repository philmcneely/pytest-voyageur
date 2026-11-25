"""
===============================================================================
Probe Sync (copy the service's page probe into the package and record its sha256)
===============================================================================
The page probe is the one piece of code that reads a page for the client, so it is vendored (src/pytest_voyageur/probe.js)
with its sha256 in probe.sha256, and tests/unit/test_probe.py holds it equal to the service's own copy. When the service's
probe changes, run this, read `git diff src/pytest_voyageur/probe.js` and commit both files together.

Usage:
    python scripts/sync_probe.py                          # from the installed `voyageur` package
    python scripts/sync_probe.py path/to/probe.js         # from a checkout of the service
    python scripts/sync_probe.py http://127.0.0.1:8765    # from a running service (GET /v1/probe.js)

Environment Variables:
    HEAL_SERVICE_TOKEN: bearer token, when the running service needs one

Author: PMAC
===============================================================================
"""
import hashlib
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "pytest_voyageur"


def fetch(source: str) -> bytes:
    if source.startswith(("http://127.0.0.1", "http://localhost", "https://")):
        headers = {"Authorization": f"Bearer {os.environ['HEAL_SERVICE_TOKEN']}"} if os.environ.get("HEAL_SERVICE_TOKEN") else {}
        with urlopen(Request(source.rstrip("/") + "/v1/probe.js", headers=headers), timeout=30) as resp:   # noqa: S310
            return resp.read()
    if source:
        return Path(source).read_bytes()
    from importlib import resources
    return (resources.files("voyageur") / "probe" / "probe.js").read_bytes()


def main() -> None:
    data = fetch(sys.argv[1] if len(sys.argv) > 1 else "")
    (PACKAGE / "probe.js").write_bytes(data)
    (PACKAGE / "probe.sha256").write_text(f"{hashlib.sha256(data).hexdigest()}  probe.js\n", encoding="utf-8")
    print(f"probe.js: {len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
