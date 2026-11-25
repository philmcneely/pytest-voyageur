"""
===============================================================================
Contract Replay (every shared fixture, driven through this package's client)
===============================================================================
tests/contract/cases is a copy of the Voyageur service's contract fixtures (SOURCE.md names the commit). Each case is one
drifted page object and one scripted conversation. The client is run against the real service, in process, with the case's
scripted model, a fake page that answers what the script's `results` say it saw, and a project on disk built from the case's
source files. The client must

    - send a request the service reads the same way as the case's own request (files, stack, inventory, last pass),
    - report exactly what the script says it saw, in order,
    - get exactly the answers recorded in expected.json (the `python` side), step by step, and
    - edit a file only when the case ends `healed`, and then to exactly the patch the service sent.

Features:
    ✓ One test per case, plus a check that the required cases are all present
    ✓ The client's own safety rules run for real (hashes, spans, validator); only the browser and the re-run are scripted

Environment Variables:
    None.

Author: PMAC
===============================================================================
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

import pytest

from tests.support import ScriptedModel, build_app

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from voyageur.schemas import InventoryItem, parse_request  # noqa: E402

from pytest_voyageur import fingerprints as fp  # noqa: E402
from pytest_voyageur.client import HealSettings, Healer, ServiceClient  # noqa: E402
from pytest_voyageur.language import page_language  # noqa: E402

CASES_DIR = Path(__file__).parent / "cases"
CASES = sorted(p for p in CASES_DIR.iterdir() if p.is_dir() and (p / "request.json").exists())
REQUIRED = {"renamed_id_to_testid", "fingerprint_match_zero_tokens", "text_pick_copy_changed", "not_drift",
            "ambiguous_source_needs_human", "target_removed_needs_human", "french_page_language_branch",
            "tier_floor_kept_id_stays_id", "tier_floor_down_flagged", "count_mismatch_next_candidate",
            "wrong_element_repick", "budget_exhausted", "prompt_injection_in_page_text"}


def summarize(resp: Dict[str, Any]) -> Dict[str, Any]:
    """The contract view of one response (the service's own tests/contract/replay.py summarize, same fields)."""
    out: Dict[str, Any] = {
        "status": resp["status"], "round": resp["round"], "reason": resp.get("reason"), "settled_at": resp.get("settled_at"),
        "ended_at": resp.get("ended_at"), "cache_hit": resp["cache_hit"],
        "usage": {k: resp["usage"][k] for k in ("calls", "input_tokens", "output_tokens")}}
    c = resp.get("candidate")
    if c:
        def patch(p: Dict[str, Any]) -> Dict[str, Any]:
            return {"file": p["file"], "from": p["from"], "to": p["to"]}
        out["candidate"] = {
            "target_ref": c["target_ref"], "strategy": c["strategy"], "tier": c["tier"], "tier_movement": c["tier_movement"],
            "below_floor": c["below_floor"], "locator": c["locator"], "code": c["code"], "branches": c["branches"],
            "expect": c["expect"], "patch": patch(c["patch"]), "also_patches": [patch(p) for p in c["also_patches"]]}
    return out


class Recording(ServiceClient):
    """The client's service connection, keeping what it sent and what came back."""

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.requests: List[Dict[str, Any]] = []
        self.reports: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []

    def heal(self, request: Dict[str, Any]) -> Dict[str, Any]:
        self.requests.append(request)
        self.responses.append(super().heal(request))
        return self.responses[-1]

    def result(self, heal_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
        self.reports.append(report)
        self.responses.append(super().result(heal_id, report))
        return self.responses[-1]


class ScriptedPage:
    """A page that says what the script's `results` say the client saw, one result per candidate tried."""

    def __init__(self, request: Dict[str, Any], results: List[Dict[str, Any]]):
        page = request["page"]
        lang = page.get("language") or {}
        self.items = [InventoryItem.model_validate(i).flat(n) for n, i in enumerate(page.get("inventory", []))]
        self.page, self.results, self.current = page, list(results), {}
        self.language = page_language(lang.get("html_lang"), lang.get("browser_locale"), lang.get("content_language"))

    def __enter__(self) -> "ScriptedPage":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def snapshot(self) -> Dict[str, Any]:
        page = self.page
        return {"path": urlsplit(page["url"]).path or "/", "inventory": self.items, "more": page.get("inventory_more", 0),
                "copy_more": page.get("inventory_copy_more", 0), "copy_withheld": page.get("inventory_copy_withheld", 0),
                "copy_withheld_data": page.get("inventory_copy_withheld_data", 0), "aria": page.get("aria_snapshot"),
                "viewport": {"w": 1280, "h": 720, "dpr": 1}, "language": page.get("language") or {}}

    def check(self, expression: str) -> Tuple[Any, Any]:
        assert self.results, "the client tried more candidates than the script has results for"
        self.current = self.results.pop(0)
        return self.current.get("count"), (self.current.get("hit") or {}).get("ref")

    def rerun(self, failure: Dict[str, Any]) -> bool:
        return self.current.get("test_outcome") == "passed"


def seed_fingerprints(root: Path, request: Dict[str, Any], page_path: str) -> None:
    """Write the last green run's record the way the plugin would have, so the client builds the case's `last_pass`."""
    last = request.get("last_pass")
    if not last:
        return
    top = request["failure"]["stack"][0]
    key = fp.call_key(top["file"], fp.line_of(root, top["file"], top["line"]))
    entries = {key: {"file": top["file"], "page": page_path, "language": last.get("language") or "",
                     "fingerprint": last["fingerprint"]}}
    for i, n in enumerate(last.get("neighbours", [])):
        entries[f"neighbour-{i:03}"] = {"file": top["file"], "page": page_path, "fingerprint": n}
    fp.merge_into(root, entries, env={"HEAL_DIR": ".heal"})


def read_case(case: Path) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    load = lambda name: json.loads((case / name).read_text(encoding="utf-8"))  # noqa: E731
    return load("request.json"), load("script.json"), load("expected.json")


def test_every_required_case_is_present():
    assert REQUIRED <= {c.name for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_client_reaches_the_recorded_answers(case, tmp_path):
    request, script, expected = read_case(case)
    files = {f["path"]: f["content"] for f in request["source"]["files"]}
    for rel, content in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(content.encode("utf-8"))
    page_path = urlsplit(request["page"]["url"]).path or "/"
    seed_fingerprints(tmp_path, request, page_path)

    http = TestClient(build_app(ScriptedModel(script.get("model", [])), **script.get("settings", {})),
                      base_url="http://127.0.0.1", headers={"Content-Type": "application/json"})
    service = Recording("http://127.0.0.1", http=http)
    page = ScriptedPage(request, script.get("results", []))
    settings = HealSettings(url="http://127.0.0.1", roots=tuple(request["source"]["roots"]) or ("pages", "tests"),
                            test_id_attribute=request.get("test_id_attribute", "data-testid"))
    healer = Healer(service, settings, page.rerun, root=tmp_path, live_factory=lambda route, probe: page, run_id="contract")
    failure = {"name": request["test"].get("title") or "test", "nodeid": request["test"]["id"],
               "error": request["failure"]["error"],
               "frames": [(str(tmp_path / f["file"]), f["line"]) for f in reversed(request["failure"]["stack"])],
               "files": [str(tmp_path / rel) for rel in files]}      # a case with no stack line still sends its files

    decision = healer.heal(failure)

    # the request: the service reads the client's the same way as the case's
    sent, wanted = parse_request(service.requests[0]), parse_request(request)
    assert [(f.path, f.content) for f in sent.source.files] == [(f.path, f.content) for f in wanted.source.files]
    assert [(f.file, f.line) for f in sent.failure.stack] == [(f.file, f.line) for f in wanted.failure.stack]
    assert sent.page.flat_inventory() == wanted.page.flat_inventory()
    assert sent.test_id_attribute == wanted.test_id_attribute
    assert (sent.last_pass.model_dump() if sent.last_pass else None) == (wanted.last_pass.model_dump() if wanted.last_pass else None)
    assert sent.page.language == wanted.page.language

    # what it reported, and what the service answered
    # (a candidate the live page rejects is never re-run, so the client sends no test_outcome for it; the script's result
    # carries one anyway because the fixtures fix the results, and the service answers the same either way)
    for n, (report, scripted) in enumerate(zip(service.reports, script.get("results", []))):
        tried = service.responses[n]["candidate"]
        rejected = report.get("count") != 1 or (report.get("hit") or {}).get("ref") != tried["expect"]["ref"]
        wanted_report = {k: v for k, v in scripted.items() if v is not None and not (rejected and k == "test_outcome")}
        assert report == wanted_report, n                          # (a null `hit` and an absent one say the same)
    assert [summarize(r) for r in service.responses] == expected["python"]

    # what it did to the files
    final = service.responses[-1]
    assert decision["outcome"] == {"healed": "healed", "not_drift": "not_drift"}.get(final["status"], "needs_human")
    after = {rel: (tmp_path / rel).read_bytes().decode("utf-8") for rel in files}
    if final["status"] == "healed":
        p = final["candidate"]["patch"]
        want = dict(files)
        want[p["file"]] = files[p["file"]][:p["span"]["start"]] + p["to"] + files[p["file"]][p["span"]["end"]:]
        if final["candidate"]["branches"]:                        # a language branch brings the helper's import with it
            want[p["file"]] = "from pytest_voyageur.language import by_language\n" + want[p["file"]]
        assert after == want
    else:
        assert after == files
