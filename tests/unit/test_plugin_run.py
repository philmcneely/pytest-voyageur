"""
===============================================================================
Plugin Runs Without a Browser (pytester: what a user's pytest run prints and does)
===============================================================================
Real pytest runs of a tiny suite with no Playwright page: the plugin's options, its marker, the header, the summary, and the
promises that matter most: without --heal nothing happens, and a service that is down or a failure that cannot be healed never
makes a run worse than plain pytest.

Features:
    ✓ The options and the `no_heal` marker are registered; `--heal` is accepted
    ✓ Without the flag: no header line, no summary, no `.heal` directory
    ✓ With the flag and no service: one header line, the failure is an ordinary failure
    ✓ With the flag and a service: a failing test with no page is reported needs_human (no_page), never an error
    ✓ Inside a heal re-run (HEAL_RUN) the plugin is inert whatever else is set

Environment Variables:
    None.

Author: PMAC
===============================================================================
"""
import pytest

pytest_plugins = ["pytester"]

FAILING = "def test_plain():\n    assert 1 == 2\n"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("HEAL_SERVICE_URL", "HEAL_SERVICE_TOKEN", "VOYAGEUR_HEAL", "HEAL_RUN", "HEAL_FINGERPRINTS", "BASE_URL",
                 "HEAL_SOURCE_ROOTS", "HEAL_DIR", "HEAL_ARTIFACTS_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_the_options_and_the_marker_are_registered(pytester):
    out = pytester.runpytest("--help").stdout.str()
    for option in ("--heal ", "--heal-url", "--heal-max", "--heal-record"):
        assert option in out
    assert "no_heal" in pytester.runpytest("--markers").stdout.str()


def test_without_the_flag_nothing_is_printed_or_written(pytester):
    pytester.makepyfile(FAILING)
    result = pytester.runpytest("-p", "no:playwright")
    result.assert_outcomes(failed=1)
    assert "voyageur:" not in result.stdout.str() and "heal summary" not in result.stdout.str()
    assert not (pytester.path / ".heal").exists()


def test_a_service_that_is_down_is_one_header_line_and_a_plain_failure(pytester, monkeypatch):
    monkeypatch.setenv("HEAL_SERVICE_URL", "http://127.0.0.1:9")
    pytester.makepyfile(FAILING)
    result = pytester.runpytest("-p", "no:playwright", "--heal")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["voyageur: --heal is on but*healing is off for this run"])
    assert "heal summary" not in result.stdout.str()


def test_the_heal_rerun_is_inert_whatever_is_set(pytester, monkeypatch):
    monkeypatch.setenv("HEAL_RUN", "1")
    monkeypatch.setenv("VOYAGEUR_HEAL", "1")
    pytester.makepyfile(FAILING)
    result = pytester.runpytest("-p", "no:playwright", "--heal", "--heal-record")
    result.assert_outcomes(failed=1)
    assert "voyageur:" not in result.stdout.str() and not (pytester.path / ".heal").exists()


def test_a_failing_test_with_no_page_is_needs_human_not_an_error(pytester, monkeypatch):
    support = pytest.importorskip("tests.support", reason="the voyageur service is not installed")
    with support.ServiceThread(support.build_app(support.ScriptedModel([]))) as svc:
        monkeypatch.setenv("HEAL_SERVICE_URL", svc.url)
        pytester.makepyfile(FAILING)
        result = pytester.runpytest("-p", "no:playwright", "--heal")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["voyageur: healing on, service http://127.0.0.1:*", "*voyageur: heal summary*",
                                 "needs a person 1", "*[[]needs_human[]] [[]no_page[]]*test_plain*no app URL*",
                                 "*hint: source files outside the source roots*HEAL_SOURCE_ROOTS*"])


def test_the_fixture_refuses_to_heal_inside_a_heal_rerun(pytester, monkeypatch):
    monkeypatch.setenv("HEAL_RUN", "1")
    pytester.makepyfile("def test_x(voyageur):\n    d = voyageur.heal(None, 'boom')\n    assert d['reason'] == 'inside_heal_run'\n")
    pytester.runpytest("-p", "no:playwright").assert_outcomes(passed=1)
