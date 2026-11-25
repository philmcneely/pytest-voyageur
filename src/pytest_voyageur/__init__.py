"""
===============================================================================
pytest-voyageur (heal drifted Playwright locators from pytest)
===============================================================================
A pytest plugin for pytest + Playwright projects. With `pytest --heal` a failing test's page is probed, a Voyageur service
picks the element the broken step meant and writes a better locator, and this package applies it under strict client-side
safety checks, re-runs the exact test and keeps the edit only when the service confirms the heal.

Features:
    ✓ plugin: the `--heal` option, the failure hook, the `voyageur` fixture and the terminal summary (pytest11 entry point)
    ✓ client: the service client, the locator validator, the patch planner and the heal loop
    ✓ fingerprints, language, redact: what the last green run saw, the by_language helper and the privacy filter
    ✓ probe.js: the vendored page probe, its sha256 in probe.sha256

Environment Variables:
    See client.py (HEAL_SERVICE_URL and the rest), plugin.py (VOYAGEUR_HEAL, HEAL_ARTIFACTS_DIR) and rerun.py (HEAL_RERUN_TIMEOUT).

Author: PMAC
===============================================================================
"""
__version__ = "0.1.0"
