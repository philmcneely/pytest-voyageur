# Contributing

Issues and pull requests are welcome. There is no agreement to sign.

## Licence of contributions

pytest-voyageur is released under the MIT licence.

**By opening a pull request, you license your contribution under the MIT licence**, to the project owner and to everyone else.
You keep the copyright in your work. You also confirm that you wrote it, or otherwise have the right to submit it under those
terms. There is no CLA.

MIT text: https://opensource.org/license/mit

## Before you open a pull request

- The tests pass: `python -m pytest -q` (see "Development" in the README for the set-up; Python 3.12 runs everything).
- `uvx ruff@0.14.14 check src tests scripts` is clean.
- A behaviour change comes with a test. A change to what is sent to the service, or to what is written to a file, comes with a
  test that shows the boundary.
- No hostnames, keys, local paths or personal data in code, docs, tests or fixtures; examples use `localhost` and `app.example`.
  `tests/unit/test_source_hygiene.py` checks this.
- `src/pytest_voyageur/probe.js` is the service's script, vendored. Do not edit it here: change it in the Voyageur service, then
  run `python scripts/sync_probe.py`.
- Screenshots in `docs/images/` are captured from real runs. Do not mock them.
