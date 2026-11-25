"""
===============================================================================
Unit Tests: plugin.py and rerun.py -- the switch, the rendering, the summary file, the re-run and the page guard
===============================================================================
No browser and no service: fake pages, fake configs and a replaced subprocess. The behaviour of the whole plugin against a real
browser and a real service is in tests/e2e.

Features:
    ✓ The switch: --heal or VOYAGEUR_HEAL=1, never inside a heal re-run
    ✓ A service string is one line when printed; heal-summary.json is written where HEAL_ARTIFACTS_DIR says
    ✓ A failure inside the heal client becomes a needs_human decision, never an exception in a test run
    ✓ The re-run: command line, options forwarded, token withheld, a hang killed, what counts as a pass
    ✓ The guard on the test's own page: foreign requests aborted only while probing, the route removed after

Environment Variables:
    None.

Author: PMAC
===============================================================================
"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pytest_voyageur import plugin, rerun
from pytest_voyageur.client import AttachedPage, HealSettings, PageGuard


def config(tmp_path, **options):
    opts = {"heal": False, "heal_url": None, "heal_max": 10, "heal_record": False, "base_url": None, **options}
    return SimpleNamespace(
        getoption=lambda name, default=None: opts.get(name, default), getini=lambda name: "", rootpath=tmp_path, inipath=None,
        pluginmanager=SimpleNamespace(hasplugin=lambda name: name in opts.get("_plugins", ())))


# ---- the switch --------------------------------------------------------------------------------------------------------
class TestSwitch:
    def test_off_by_default(self, tmp_path, monkeypatch):
        for name in ("VOYAGEUR_HEAL", "HEAL_RUN", "HEAL_FINGERPRINTS"):
            monkeypatch.delenv(name, raising=False)
        assert plugin.heal_requested(config(tmp_path)) is False and plugin.record_requested(config(tmp_path)) is False

    def test_the_flag_and_the_environment_turn_it_on(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HEAL_RUN", raising=False)
        assert plugin.heal_requested(config(tmp_path, heal=True)) is True
        monkeypatch.setenv("VOYAGEUR_HEAL", "On")
        assert plugin.heal_requested(config(tmp_path)) is True
        monkeypatch.setenv("VOYAGEUR_HEAL", "0")
        assert plugin.heal_requested(config(tmp_path)) is False

    def test_nothing_is_on_inside_the_rerun_of_a_heal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEAL_RUN", "1")
        monkeypatch.setenv("VOYAGEUR_HEAL", "1")
        monkeypatch.setenv("HEAL_FINGERPRINTS", "on")
        assert plugin.heal_requested(config(tmp_path, heal=True)) is False
        assert plugin.record_requested(config(tmp_path, heal_record=True)) is False


# ---- rendering --------------------------------------------------------------------------------------------------------
class TestRendering:
    def test_a_service_string_is_one_line(self):
        lines = plugin.describe({"test": "t::x", "outcome": "needs_human", "reason": "r",
                                 "why": "line one\nFAKE: [healed] injected\r\n- bullet\x1b[31m", "tries": []})
        assert len(lines) == 1 and "\n" not in lines[0] and "\x1b" not in lines[0]

    def test_no_field_of_a_decision_can_start_a_new_line(self):
        evil = "x\n[healed] fake: a.py: a -> b"
        lines = plugin.describe({"test": evil, "outcome": evil, "reason": evil, "why": evil, "file": evil, "hint": evil,
                                 "tries": [{"code": evil, "file": evil, "result": evil, "why": evil}]})
        assert all("\n" not in line for line in lines)
        healed = plugin.describe({"test": "t", "outcome": "healed", "file": evil, "from": evil, "to": evil, "source": evil,
                                  "tries": [{"code": evil, "file": evil, "result": evil}]})
        assert all("\n" not in line for line in healed) and len(healed) == 2

    def test_a_healed_decision_names_file_before_after_and_each_try(self):
        lines = plugin.describe({"test": "t::x", "outcome": "healed", "file": "pages/p.py", "from": "a", "to": "b",
                                 "source": "heal service (settled at step 2)",
                                 "tries": [{"code": "b", "file": "pages/p.py", "result": "kept", "count": 1}]})
        assert lines[0].startswith("[healed] t::x: pages/p.py: a -> b") and "try 1: b in pages/p.py: kept (count=1)" in lines[1]

    def test_the_hint_about_files_outside_the_roots_is_shown(self):
        lines = plugin.describe({"test": "t", "outcome": "needs_human", "why": "w", "hint": "set HEAL_SOURCE_ROOTS", "tries": []})
        assert any("hint: set HEAL_SOURCE_ROOTS" in line for line in lines)


class TestSummaryFile:
    def decisions(self):
        return [{"test": "a", "outcome": "healed", "usage": {"calls": 1, "input_tokens": 10, "output_tokens": 2}},
                {"test": "b", "outcome": "needs_human", "usage": {}}]

    def test_written_into_the_heal_dir_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HEAL_ARTIFACTS_DIR", raising=False)
        path = plugin.write_summary(tmp_path, self.decisions(), "now", "run1")
        assert path == tmp_path / ".heal" / "heal-summary.json"
        data = json.loads(path.read_text())
        assert data["counts"] == {"healed": 1, "needs_human": 1, "not_drift": 0} and data["usage"]["input_tokens"] == 10

    def test_the_artifacts_dir_moves_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEAL_ARTIFACTS_DIR", str(tmp_path / "out"))
        assert plugin.write_summary(tmp_path, self.decisions(), "now", "r") == tmp_path / "out" / "heal-summary.json"

    def test_the_printed_path_is_relative_inside_the_rootdir_and_absolute_outside(self, tmp_path):
        root = tmp_path / "repo"
        (root / ".heal").mkdir(parents=True)
        inside = root / ".heal" / "heal-summary.json"
        outside = tmp_path / "elsewhere" / "heal-summary.json"
        assert plugin.display_path(inside, root) == str(Path(".heal") / "heal-summary.json")
        assert plugin.display_path(outside, root) == str(outside)
        assert plugin.display_path(tmp_path / "repo-other" / "x.json", root) == str(tmp_path / "repo-other" / "x.json")

    def test_decisions_are_read_from_the_reports_of_every_worker(self):
        rep = SimpleNamespace(user_properties=[("other", "x"), (plugin.PROPERTY, json.dumps({"test": "a", "outcome": "healed"}))])
        junk = SimpleNamespace(user_properties=[(plugin.PROPERTY, "{not json")])
        tr = SimpleNamespace(stats={"failed": [rep, junk], "passed": [SimpleNamespace(user_properties=[])]})
        assert plugin.decisions_of(tr) == [{"test": "a", "outcome": "healed"}]


# ---- the pages ---------------------------------------------------------------------------------------------------------
class SyncPage:
    url = "http://app.example/account"

    def __init__(self, closed=False):
        self.closed, self.routes = closed, []

    def evaluate(self, script):
        return {"items": [], "more": 0, "copy_more": 4, "copy_withheld": 5, "copy_withheld_data": 6, "page": {"lang": "fr"}}

    def route(self, pattern, handler):
        self.routes.append(handler)

    def unroute(self, pattern, handler):
        self.routes.remove(handler)

    def is_closed(self):
        return self.closed

    def locator(self, sel):
        raise RuntimeError("no aria here")


class AsyncPage(SyncPage):
    async def evaluate(self, script):         # noqa: D102
        return {}


class TestUsablePage:
    def test_only_a_live_sync_page_counts(self):
        assert plugin.usable_page(SyncPage()) is True
        assert plugin.usable_page(SyncPage(closed=True)) is False
        assert plugin.usable_page(AsyncPage()) is False
        assert plugin.usable_page(None) is False

    def test_the_probes_counts_come_through_the_attached_page(self):
        view = AttachedPage(HealSettings(), SyncPage(), "(only) => 1")
        snap = view.snapshot()
        assert (snap["copy_more"], snap["copy_withheld"], snap["copy_withheld_data"]) == (4, 5, 6)
        assert snap["language"]["html_lang"] == "fr" and view.language == "fr" and snap["aria"] is None


class Route:
    def __init__(self, url):
        self.request, self.did = SimpleNamespace(url=url), []

    def abort(self):
        self.did.append("abort")

    def fallback(self):
        self.did.append("fallback")

    def continue_(self):
        self.did.append("continue")


class TestPageGuard:
    def test_the_route_exists_only_while_probing_and_foreign_requests_are_aborted(self):
        page = SyncPage()
        guard = PageGuard(page)
        with guard.active():
            assert len(page.routes) == 1 and guard.origin == ("http", "app.example", 80)
            same, foreign = Route("http://app.example/api"), Route("https://evil.example/x")
            guard.handle(same)
            guard.handle(foreign)
            assert same.did == ["fallback"] and foreign.did == ["abort"]
        assert page.routes == [] and guard.probing is False

    def test_the_route_is_removed_when_the_probe_raises(self):
        page = SyncPage()
        with pytest.raises(RuntimeError):
            with PageGuard(page).active():
                raise RuntimeError("probe failed")
        assert page.routes == []

    def test_a_known_base_url_fixes_the_origin(self):
        guard = PageGuard(SyncPage(), "https://other.example")
        with guard.active():
            assert guard.origin == ("https", "other.example", 443)

    def test_requests_pass_through_untouched_outside_a_probe(self):
        guard = PageGuard(SyncPage(), "http://app.example")
        route = Route("https://cdn.example/lib.js")
        guard.handle(route)
        assert route.did == ["fallback"]


# ---- a failure inside the client never reaches the test run ---------------------------------------------------------
class TestHealStateNeverRaises:
    def state(self, tmp_path, **options):
        return plugin.HealState(config(tmp_path, heal=True, **options))

    def excinfo(self):
        try:
            raise AssertionError("boom")
        except AssertionError:
            return pytest.ExceptionInfo.from_current()

    def test_a_bug_in_the_client_becomes_a_needs_human_decision(self, tmp_path, monkeypatch):
        state = self.state(tmp_path)
        monkeypatch.setattr(plugin.Healer, "heal", lambda self, failure: (_ for _ in ()).throw(ZeroDivisionError("x")))
        d = state.heal("tests/t.py::test_a", "test_a", self.excinfo(), None)
        assert d["outcome"] == "needs_human" and d["reason"] == "client_error" and "ZeroDivisionError" in d["why"]

    def test_failures_past_the_max_are_listed_not_dropped(self, tmp_path):
        d = self.state(tmp_path, heal_max=0).heal("tests/t.py::test_a", "test_a", self.excinfo(), None)
        assert d["outcome"] == "needs_human" and d["reason"] == "beyond_max"

    def test_no_page_and_no_app_url_is_said_plainly(self, tmp_path, monkeypatch):
        state = self.state(tmp_path)
        monkeypatch.delenv("BASE_URL", raising=False)

        class Svc:
            def probe_js(self):
                return "(only) => 1"
        state._service = Svc()
        d = state.heal("tests/t.py::test_a", "test_a", self.excinfo(), None)
        assert d["reason"] == "no_page" and "no app URL" in d["why"]

    def test_files_outside_the_source_roots_are_named_in_a_hint(self, tmp_path):
        (tmp_path / "test_root_level.py").write_text("x = 1\n")
        state = self.state(tmp_path)
        assert state.outside_roots([(str(tmp_path / "test_root_level.py"), 1)]) == ["test_root_level.py"]
        assert state.outside_roots([(str(tmp_path / ".venv" / "lib" / "x.py"), 1), ("/usr/lib/x.py", 2)]) == []

    def test_the_directories_of_the_collected_tests_become_source_roots(self, tmp_path):
        state = self.state(tmp_path)
        (tmp_path / "e2e").mkdir()
        (tmp_path / "e2e" / "test_x.py").write_text("")
        state.collect_dirs([SimpleNamespace(path=tmp_path / "e2e" / "test_x.py"), SimpleNamespace(path=tmp_path / "test_top.py")])
        assert "e2e" in state.settings.roots and "tests" in state.settings.roots


# ---- the re-run -----------------------------------------------------------------------------------------------------
CASES_OK = [{"name": "t", "classname": "c", "outcome": "passed", "message": ""}]


class TestJudge:
    def test_a_pass_needs_exit_zero_and_every_case_passed(self):
        assert rerun.judge(0, CASES_OK).passed is True and bool(rerun.judge(0, CASES_OK)) is True

    @pytest.mark.parametrize("code, cases, text", [
        (0, None, "no readable report"), (0, [], "selected no test"),
        (0, [{**CASES_OK[0], "outcome": "skipped"}], "skipped"),
        (1, [{**CASES_OK[0], "outcome": "failed", "message": "assert  1\n == 2"}], "failed: assert 1 == 2"),
        (2, CASES_OK, "exited with 2"),
    ])
    def test_everything_else_is_a_fail_with_the_reason(self, code, cases, text):
        result = rerun.judge(code, cases)
        assert result.passed is False and text in result.detail

    def test_the_report_file_is_read(self, tmp_path):
        xml = tmp_path / "r.xml"
        xml.write_text('<testsuites><testsuite><testcase classname="c" name="t"/><testcase classname="c" name="u">'
                       '<failure message="m"/></testcase><testcase name="v"><skipped/></testcase></testsuite></testsuites>')
        assert [c["outcome"] for c in rerun.read_report(xml)] == ["passed", "failed", "skipped"]
        assert rerun.read_report(tmp_path / "none.xml") is None


class TestRunExact:
    def cfg(self, tmp_path, **options):
        return config(tmp_path, **options)

    def test_the_command_runs_the_one_node_in_a_fresh_process_with_the_playwright_options(self, tmp_path):
        cfg = self.cfg(tmp_path, base_url="http://app.example", browser=["firefox"], headed=True, _plugins=("xdist", "rerunfailures"))
        cmd = rerun.command(cfg, "tests/t.py::test_a[chromium]", tmp_path / "r.xml")
        assert cmd[1:4] == ["-m", "pytest", "tests/t.py::test_a[chromium]"]
        assert ["--base-url", "http://app.example"] == cmd[cmd.index("--base-url"):cmd.index("--base-url") + 2]
        assert "--headed" in cmd and ["--browser", "firefox"] == cmd[cmd.index("--browser"):cmd.index("--browser") + 2]
        assert ["-n", "0"] == cmd[cmd.index("-n"):cmd.index("-n") + 2] and ["--reruns", "0"] == cmd[cmd.index("--reruns"):cmd.index("--reruns") + 2]
        assert f"--junit-xml={tmp_path / 'r.xml'}" in cmd and "--rootdir" in cmd

    def test_the_child_is_marked_and_never_gets_the_service_token(self, tmp_path, monkeypatch):
        seen = {}

        def fake_run(cmd, cwd, env, capture_output, timeout):
            seen.update(cwd=cwd, env=env, timeout=timeout)
            Path(next(a for a in cmd if a.startswith("--junit-xml=")).split("=", 1)[1]).write_text(
                '<testsuite><testcase classname="c" name="t"/></testsuite>')
            return SimpleNamespace(returncode=0)
        monkeypatch.setattr(subprocess, "run", fake_run)
        res = rerun.run_exact(self.cfg(tmp_path), "t.py::t", tmp_path, timeout=7,
                              env={"HEAL_SERVICE_TOKEN": "secret", "PATH": "/bin", "HEAL_SERVICE_URL": "http://x"})
        assert res.passed is True and seen["env"]["HEAL_RUN"] == "1" and "HEAL_SERVICE_TOKEN" not in seen["env"]
        assert seen["cwd"] == str(tmp_path) and seen["timeout"] == 7

    def test_a_hung_rerun_is_a_fail(self, tmp_path, monkeypatch):
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        monkeypatch.setattr(subprocess, "run", hang)
        res = rerun.run_exact(self.cfg(tmp_path), "t.py::t", tmp_path, timeout=3, env={})
        assert res.passed is False and "killed after 3s" in res.detail

    def test_a_missing_report_is_a_fail_even_with_exit_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
        assert rerun.run_exact(self.cfg(tmp_path), "t.py::t", tmp_path, env={}).passed is False
