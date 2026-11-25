"""
===============================================================================
Unit Tests: findings of the independent security read (each written to fail before its fix)
===============================================================================
A reviewer who saw only the code, not its author's conclusions, tried to break every rule the client claims and found what is
below. One test per finding: an endless service, a missing expectation, a decision that says what the reply said and not what
was written, a signal during the re-run, a revert that would destroy the user's own edit, a hostile usage block, line-faking
characters, a quadratic regex, an unquoted id in a URL path, a symlink to a secret, an oversized replacement, typed text and
URL secrets in the page text.

Features:
    ✓ The heal loop ends after MAX_TRIES candidates and after the time budget, with the file as it was
    ✓ A candidate with no expectation, or whose hit cannot be named, is never applied
    ✓ The decision reports the candidate the client tried and the files it kept, not what the last reply claims
    ✓ SIGTERM during the re-run reverts the edit; a revert never overwrites a file someone else changed since
    ✓ Garbage usage, bidi characters, a heal id with a slash, a symlink to a secret and a 5 MB `to` are all contained
    ✓ Typed values are scrubbed from the aria snapshot wherever the page holds them; URL queries are cut from aria and errors

Environment Variables:
    None.

Author: PMAC
===============================================================================
"""
import os
import signal
import time

import pytest

from pytest_voyageur import client as hc
from pytest_voyageur import plugin
from pytest_voyageur.client import HealServiceError, ServiceClient, run_total, stack_from_error
from pytest_voyageur.redact import redact_urls, scrub_values
from tests.unit.test_client import FAILURE, FROM, PAGE_SRC, TEST_SRC, TO, FakeLive, FakeService, Verify, candidate, healer, reply


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pages").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pages" / "login_page.py").write_text(PAGE_SRC)
    (tmp_path / "tests" / "test_login.py").write_text(TEST_SRC)
    return tmp_path


def original(project):
    return (project / "pages" / "login_page.py").read_bytes()


class TestRoundCap:
    def test_a_service_that_always_answers_try_ends_after_the_cap(self, project):
        svc = FakeService(*[reply("try", candidate(project), hid=f"h_{i}", rnd=i) for i in range(40)])
        verify = Verify(*[False] * 40, root=project)
        before = original(project)
        d = healer(project, svc, verify).heal(FAILURE)
        assert verify.calls <= hc.MAX_TRIES and d["outcome"] == "needs_human" and d["reason"] == "too_many_rounds"
        assert original(project) == before

    def test_the_time_budget_ends_a_slow_heal_between_tries(self, project, monkeypatch):
        svc = FakeService(*[reply("try", candidate(project), hid=f"h_{i}") for i in range(10)])
        verify = Verify(*[False] * 10, root=project)
        clock = iter(range(0, 10_000, 400))                         # every reading of the clock is 400 s later
        h = healer(project, svc, verify)
        h.clock = lambda: next(clock)
        d = h.heal(FAILURE)
        assert d["reason"] == "time_budget" and verify.calls == 2


class TestTheExpectedElement:
    def run(self, project, cand, checks):
        svc = FakeService(reply("try", cand), reply("needs_human", reason="x", why="y"))
        verify = Verify(True, root=project)
        before = original(project)
        d = healer(project, svc, verify, live=FakeLive(checks=checks)).heal(FAILURE)
        return d, verify, before

    def test_no_expectation_at_all_never_applies(self, project):
        cand = candidate(project)
        del cand["expect"]
        d, verify, before = self.run(project, cand, {TO: (1, None)})
        assert verify.calls == 0 and original(project) == before

    def test_a_hit_that_cannot_be_named_never_applies(self, project):
        cand = candidate(project)
        cand["expect"] = {"count": 1}
        d, verify, before = self.run(project, cand, {TO: (1, None)})
        assert verify.calls == 0 and original(project) == before

    def test_a_missing_expectation_against_a_named_hit_never_applies(self, project):
        cand = candidate(project)
        cand["expect"] = {}
        d, verify, before = self.run(project, cand, {TO: (1, "e0")})
        assert verify.calls == 0 and original(project) == before


class TestTheDecisionIsWhatWasWritten:
    def test_the_decision_names_the_candidate_that_was_tried_not_the_one_in_the_last_reply(self, project):
        lie = candidate(project)
        lie["patch"] = {**lie["patch"], "file": "tests/test_login.py", "from": "x", "to": 'page.get_by_text("Pay")'}
        svc = FakeService(reply("try", candidate(project)), reply("healed", lie))
        d = healer(project, svc, Verify(True, root=project)).heal(FAILURE)
        assert d["outcome"] == "healed" and d["file"] == "pages/login_page.py" and d["to"] == TO and d["from"] == FROM
        assert d["files"] == ["pages/login_page.py"] and TO in (project / "pages" / "login_page.py").read_text()

    def test_healed_with_no_candidate_still_reports_what_was_kept(self, project):
        svc = FakeService(reply("try", candidate(project)), reply("healed", None))
        d = healer(project, svc, Verify(True, root=project)).heal(FAILURE)
        assert d["outcome"] == "healed" and d["file"] == "pages/login_page.py" and TO in (project / "pages" / "login_page.py").read_text()

    def test_a_reply_missing_keys_after_a_keep_cannot_turn_a_kept_edit_into_a_crash(self, project):
        svc = FakeService(reply("try", candidate(project)), reply("healed", {"patch": {}}))
        d = healer(project, svc, Verify(True, root=project)).heal(FAILURE)
        assert d["outcome"] == "healed" and d["to"] == TO


class TestInterruptedRerun:
    def test_sigterm_during_the_rerun_puts_the_file_back(self, project):
        svc = FakeService(reply("try", candidate(project)))
        before = original(project)

        def terminated(test):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)                                               # the handler raises before this returns
            return True
        previous = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit):
            healer(project, svc, terminated).heal(FAILURE)
        assert original(project) == before and signal.getsignal(signal.SIGTERM) == previous

    def test_a_revert_never_overwrites_what_someone_else_wrote_in_the_meantime(self, project):
        svc = FakeService(reply("try", candidate(project)))
        page = project / "pages" / "login_page.py"

        def user_edits_then_fails(test):
            page.write_text(page.read_text() + "# my own change\n")
            return False
        d = healer(project, svc, user_edits_then_fails).heal(FAILURE)
        assert "# my own change" in page.read_text()                    # their work is still there
        assert d["reason"] == "client_error" and "changed since" in d["why"]

    def test_a_revert_of_an_untouched_edit_still_works(self, project):
        svc = FakeService(reply("try", candidate(project)), reply("needs_human", reason="x", why="y"))
        before = original(project)
        healer(project, svc, Verify(False, root=project)).heal(FAILURE)
        assert original(project) == before


class TestContainment:
    def test_garbage_usage_is_zeroes_and_the_run_total_still_adds_up(self, project):
        svc = FakeService(reply("not_drift", usage={"calls": "x", "input_tokens": [1], "output_tokens": 10 ** 30, "images": True}))
        d = healer(project, svc, Verify()).heal(FAILURE)
        assert d["usage"] == {"calls": 0, "input_tokens": 0, "output_tokens": 0, "images": 0}
        assert run_total([d, {"usage": {"calls": "x", "input_tokens": 3}}])["input_tokens"] == 3

    def test_bidi_and_format_characters_cannot_reorder_a_printed_line(self):
        assert "‮" not in plugin._one_line("fine‮⁦txt​")

    def test_the_stack_regex_is_linear_on_blank_lines(self):
        start = time.perf_counter()
        stack_from_error("\n" * 60_000 + "x", ("pages",), __import__("pathlib").Path("."))
        assert time.perf_counter() - start < 1.0

    @pytest.mark.parametrize("heal_id", ["../../v1/audit", "h_1/../x", "a b", "", "h" * 65, "h_1?x=1", "h_1\n"])
    def test_a_heal_id_with_a_slash_or_anything_odd_is_never_put_in_a_url(self, heal_id):
        import httpx
        seen = []
        client = ServiceClient("https://svc", "tok", httpx.Client(transport=httpx.MockTransport(
            lambda r: seen.append(r.url.path) or httpx.Response(200, json={}))))
        with pytest.raises(HealServiceError):
            client.result(heal_id, {"count": 1})
        assert seen == []

    def test_a_symlink_to_a_secret_inside_the_repo_is_not_sent_even_with_the_whole_repo_as_root(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / ".env").write_text("SECRET=1\n")
        (tmp_path / "tests" / "leak.py").symlink_to(tmp_path / ".env")
        got = hc.collect_source([{"file": "tests/leak.py", "line": 1}], (".",), tmp_path)
        assert got == []

    def test_a_replacement_over_the_services_own_limit_is_refused(self):
        too_long = 'self.page.get_by_text("' + "a" * 5000 + '")'
        assert hc.validate_replacement(too_long, FROM) is None


class TestPageText:
    def test_typed_values_are_scrubbed_wherever_the_aria_snapshot_shows_them(self):
        aria = '- generic "Rich": TYPED-ce\n- text: TYPED-ce\n- paragraph: line two typed\n- link "Home"\n'
        out = scrub_values(aria, ["TYPED-ce", "line two typed"])
        assert "TYPED-ce" not in out and "line two typed" not in out and '- link "Home"' in out

    def test_short_values_are_left_alone_and_the_scrub_is_bounded(self):
        assert scrub_values("- text: 1 of 2", ["1", "2", ""]) == "- text: 1 of 2"
        assert scrub_values("x" * 10, ["x" * 3] * 5000) == "x"            # many values: still finishes

    def test_the_query_and_fragment_of_a_link_in_the_aria_snapshot_are_cut(self):
        aria = '- link "Reset":\n  - /url: https://app.example/reset?token=SECRET123&u=bob#frag\n  - /url: /next?code=abc\n'
        out = redact_urls(aria)
        assert "SECRET123" not in out and "bob" not in out and "frag" not in out and "code=abc" not in out
        assert "- /url: https://app.example/reset" in out and "- /url: /next" in out

    def test_urls_in_the_error_text_lose_their_query_and_fragment(self):
        err = 'navigating to "http://app.example/cb?code=abc123&state=s#frag", waiting\nlocator("#a")'
        out = hc.bounded_error(err)
        assert "abc123" not in out and "frag" not in out and 'navigating to "http://app.example/cb"' in out
        assert 'locator("#a")' in out
