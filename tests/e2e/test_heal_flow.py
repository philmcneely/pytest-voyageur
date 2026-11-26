"""
===============================================================================
End-to-end: `pytest --heal` on a tiny Playwright suite with a drifted locator
===============================================================================
A pytester child process runs a real suite (a page object, a test, pytest-playwright, Chromium) against a local site whose id
was renamed. The real Voyageur service answers in this process from a scripted model. The tests hold the plugin to what a user
sees: the file is edited and the edit survives, a wrong candidate is reverted, nothing happens without --heal, a service that is
down never fails the run, a shared page object is healed once, the language branch reads on both sites, and a green run's
fingerprints settle the next heal with no model call.

Features:
    ✓ Healed: the locator in the page object changes, a second run without --heal passes, heal-summary.json lists the heal
    ✓ Wrong candidate: reverted, byte for byte, then the right one is kept; and when every pick is wrong the file is untouched
    ✓ Inert without --heal (the service is never asked); VOYAGEUR_HEAL=1 is the same switch as the flag
    ✓ Service unreachable: one line in the header, the run goes on as plain pytest
    ✓ Shared page object, parallel workers (pytest-xdist), the `voyageur` fixture's audit, fingerprints, language branch

Environment Variables:
    None.

Author: PMAC
===============================================================================
"""
import json

import pytest

from tests.e2e.conftest import ACCOUNT_PAGE, PAGE_DRIFTED, needs_chromium
from tests.support import RuleModel, ScriptedModel, pick

pytestmark = [pytest.mark.browser, needs_chromium]
HEALED = 'self.page.get_by_test_id("login-user").fill(name)'


def test_a_drifted_locator_is_healed_and_the_edit_stays(project, service):
    project.site.set("account.html", PAGE_DRIFTED)
    svc, model = service(ScriptedModel([pick("e0")]))
    before = project.read("pages/account_page.py")

    result = project.run("--heal", service=svc)

    assert HEALED in project.read("pages/account_page.py")
    assert project.read("pages/account_page.py") == before.replace("self.page.locator('#username').fill(name)", HEALED)
    result.stdout.fnmatch_lines(["*[[]healed[]] tests/test_account.py::test_fill_name*#username*->*login-user*",
                                 "*try 1:*kept*", "*voyageur: heal summary*", "*healed 1*"])
    assert result.ret == 1                                       # the run that needed the heal still reports its failure
    summary = json.loads((project.root / ".heal" / "heal-summary.json").read_text())
    assert summary["counts"]["healed"] == 1 and summary["usage"]["calls"] == 1
    assert len(model.prompts) == 1 and "hunter2" not in model.prompts[0]
    again = project.run()                                        # the next plain run is green
    again.assert_outcomes(passed=1)


def test_the_environment_switch_is_the_same_as_the_flag(project, service):
    project.site.set("account.html", PAGE_DRIFTED)
    svc, _ = service(ScriptedModel([pick("e0")]))
    project.run(service=svc, env={"VOYAGEUR_HEAL": "1"})
    assert HEALED in project.read("pages/account_page.py")


def test_a_wrong_candidate_is_reverted_and_the_file_is_left_exactly_as_it_was(project, service):
    project.site.set("account.html", PAGE_DRIFTED)
    svc, model = service(ScriptedModel([pick("e1")]))          # the confirm field: a decoy the service accepts as plausible
    before = (project.root / "pages" / "account_page.py").read_bytes()

    result = project.run("--heal", service=svc)

    assert (project.root / "pages" / "account_page.py").read_bytes() == before
    assert not list(project.root.rglob("*.heal-tmp"))
    out = result.stdout.str()
    assert out.count("reverted") >= 2 and "login-user-confirm" in out        # every locator written for it was tried and reverted
    result.stdout.fnmatch_lines(["*needs a person 1*", "*[[]needs_human[]]*test_fill_name*"])
    assert json.loads((project.root / ".heal" / "heal-summary.json").read_text())["counts"] == \
        {"healed": 0, "needs_human": 1, "not_drift": 0}
    assert len(model.prompts) == 1


def test_without_the_flag_the_plugin_does_nothing(project, service):
    project.site.set("account.html", PAGE_DRIFTED)
    svc, model = service(ScriptedModel([pick("e0")]))
    before = project.read("pages/account_page.py")

    result = project.run(service=svc)

    result.assert_outcomes(failed=1)
    assert project.read("pages/account_page.py") == before and model.prompts == []
    assert "voyageur:" not in result.stdout.str() and "heal summary" not in result.stdout.str()
    assert not (project.root / ".heal").exists()


def test_a_service_that_is_down_never_fails_the_run(project):
    project.site.set("account.html", PAGE_DRIFTED)
    before = project.read("pages/account_page.py")

    result = project.run("--heal", env={"HEAL_SERVICE_URL": "http://127.0.0.1:9"})

    result.assert_outcomes(failed=1)                             # an ordinary failure, nothing worse
    result.stdout.fnmatch_lines(["*voyageur: --heal is on but*could not be reached*healing is off*"])
    assert project.read("pages/account_page.py") == before


def test_a_marked_test_is_never_healed(project, service):
    project.site.set("account.html", PAGE_DRIFTED)
    project.write("tests/test_account.py", "import pytest\n\npytestmark = pytest.mark.no_heal\n\n" + project.read("tests/test_account.py"))
    svc, model = service(ScriptedModel([pick("e0")]))
    project.run("--heal", service=svc)
    assert "#username" in project.read("pages/account_page.py") and model.prompts == []


def test_a_page_object_shared_by_two_tests_is_healed_once(project, service):
    project.site.set("account.html", PAGE_DRIFTED)
    project.write("tests/test_account.py", project.read("tests/test_account.py") + "\n\n"
                  + project.read("tests/test_account.py").split("\n\n\n", 1)[1].replace("test_fill_name", "test_fill_name_again"))
    svc, model = service(ScriptedModel([pick("e0"), pick("e0")]))

    result = project.run("--heal", service=svc)

    assert project.read("pages/account_page.py").count("login-user") == 1
    result.stdout.fnmatch_lines(["*healed 2*", "*same fix as*test_fill_name*shared locator*"])
    assert len(model.prompts) == 1                               # the second test cost no model call


def test_parallel_workers_heal_one_file_at_a_time(project, service):
    pytest.importorskip("xdist")
    project.site.set("account.html", PAGE_DRIFTED)
    project.write("pages/password_page.py", ACCOUNT_PW)
    project.write("tests/test_password.py", TEST_PW)
    svc, model = service(RuleModel([("fill_name", "e0"), ("fill_password", "e2")]))

    result = project.run("--heal", "-n", "2", service=svc)

    assert HEALED in project.read("pages/account_page.py")
    assert "#password" not in project.read("pages/password_page.py")
    result.stdout.fnmatch_lines(["*healed 2*"])


ACCOUNT_PW = '''\
class PasswordPage:
    def __init__(self, page):
        self.page = page

    def fill_password(self, text):
        self.page.locator('#password').fill(text)
'''
TEST_PW = '''\
from pages.account_page import AccountPage
from pages.password_page import PasswordPage


def test_fill_password(page, base_url):
    AccountPage(page, base_url).open()
    PasswordPage(page).fill_password("x")
    assert page.locator("#pw").input_value() == "x"
'''


def test_a_green_run_records_what_the_next_heal_needs_so_it_costs_no_model_call(project, service):
    green, _ = service(ScriptedModel([]))
    first = project.run("--heal-record", service=green)
    first.assert_outcomes(passed=1)
    store = json.loads((project.root / ".heal" / "fingerprints.json").read_text())
    assert any(k.startswith("pages/account_page.py::self.page.locator('#username').fill(name)") for k in store["entries"])
    assert (project.root / ".heal" / ".gitignore").read_text() == "*\n"

    project.site.set("account.html", PAGE_DRIFTED)               # the id is renamed; the test id is not
    svc, model = service(ScriptedModel([]))
    project.run("--heal", service=svc)

    assert HEALED in project.read("pages/account_page.py")
    assert model.prompts == []                                   # settled by the recorded element, no model call
    summary = json.loads((project.root / ".heal" / "heal-summary.json").read_text())
    assert summary["usage"]["calls"] == 0 and summary["decisions"][0]["settled_at"] == 2


def test_the_voyageur_fixture_audits_a_page_with_no_model_call(project, service):
    project.site.set("account.html", PAGE_DRIFTED)
    project.write("tests/test_audit.py", AUDIT_TEST)
    svc, model = service(ScriptedModel([]))
    result = project.run("tests/test_audit.py", service=svc)
    result.assert_outcomes(passed=1)
    assert model.prompts == []


AUDIT_TEST = '''\
def test_audit(page, base_url, voyageur):
    page.goto(f"{base_url}/account.html")
    rows = voyageur.audit(page)["locators"]
    by_call = {r["call"]: r for r in rows}
    assert by_call["page.locator('#username')"]["status"] == "missing"          # the id that drifted away
    assert by_call["page.get_by_test_id('login-user')"]["status"] == "ok"
    page.locator('#username')
    page.get_by_test_id('login-user')
'''


def test_a_translated_page_gets_a_language_branch_that_reads_on_both_sites(project, service):
    en = '<html lang="en"><body><input aria-label="Username"><button>Save</button></body></html>'
    fr = '<html lang="fr"><body><input aria-label="Nom d\'utilisateur"><button>Enregistrer</button></body></html>'
    project.site.set("account.html", en)
    project.write("pages/account_page.py", ACCOUNT_PAGE.replace("self.page.locator('#username')", "self.page.get_by_label(\"Username\")"))
    project.write("tests/test_account.py", TEST_VALUE)
    green, _ = service(ScriptedModel([]))
    project.run("--heal-record", service=green).assert_outcomes(passed=1)       # the English run is recorded

    project.site.set("account.html", fr)
    svc, _ = service(ScriptedModel([pick("e0")]))
    project.run("--heal", service=svc)

    text = project.read("pages/account_page.py")
    assert "by_language(self.page, {" in text and '"en"' in text.replace("'", '"') and "Nom d" in text
    assert "from pytest_voyageur.language import by_language" in text
    project.run().assert_outcomes(passed=1)                                     # the French site passes on a plain run
    project.site.set("account.html", en)
    project.run().assert_outcomes(passed=1)                                     # and so does the English one


TEST_VALUE = '''\
from pages.account_page import AccountPage


def test_fill_name(page, base_url):
    account = AccountPage(page, base_url)
    account.open()
    account.fill_name("Ada")
    assert page.evaluate("document.querySelector('input').value") == "Ada"
'''


# ---- an element inside an iframe ---------------------------------------------------------------------------------------
PAY_FRAME = ('<html lang="en"><body><form><label for="card">Card number</label><input id="card" name="card" data-testid="card-no">'
             '<button type="submit">Pay</button></form></body></html>')
PAY_FRAME_DRIFTED = PAY_FRAME.replace('name="card"', 'name="cc-number"').replace('id="card"', 'id="cc-number"')
PAY_PAGE = '<html lang="en"><body><h1>Pay</h1><iframe name="payment" src="/pay-frame.html"></iframe></body></html>'
PAY_OBJECT = '''\
class PayPage:
    def __init__(self, page, base_url):
        self.page = page
        self.url = f"{base_url}/pay.html"

    def open(self):
        self.page.goto(self.url)

    def fill_card(self, number):
        self.page.frame_locator('iframe[name="payment"]').locator('input[name=card]').fill(number)
'''
PAY_TEST = '''\
from pages.pay_page import PayPage


def test_fill_card(page, base_url):
    pay = PayPage(page, base_url)
    pay.open()
    pay.fill_card("4242")
    assert page.frame_locator("iframe").locator("input").input_value() == "4242"
'''


def test_a_drifted_field_inside_an_iframe_is_healed_and_the_edit_stays(project, service):
    project.site.set("pay.html", PAY_PAGE)
    project.site.set("pay-frame.html", PAY_FRAME_DRIFTED)
    project.write("pages/pay_page.py", PAY_OBJECT)
    project.write("tests/test_pay.py", PAY_TEST)
    svc, model = service(ScriptedModel([pick("e3")]))

    result = project.run("--heal", "tests/test_pay.py", service=svc)

    healed = project.read("pages/pay_page.py")
    assert "frame_locator('iframe[name=\"payment\"]').locator(" in healed or "frame_locator(" in healed
    assert "input[name=card]" not in healed and "card-no" in healed, healed
    result.stdout.fnmatch_lines(["*[[]healed[]]*test_fill_card*", "*try 1:*kept*"])
    project.run("tests/test_pay.py").assert_outcomes(passed=1)


def test_a_framed_element_is_recorded_on_the_top_pages_route(site):
    """The route a heal reopens when the failing page is gone is the top page's, not the frame document's (no iframe in it)."""
    from playwright.sync_api import sync_playwright
    from pytest_voyageur.client import vendored_probe
    from pytest_voyageur.fingerprints import Recorder
    from pathlib import Path
    site.set("pay.html", PAY_PAGE)
    site.set("pay-frame.html", PAY_FRAME)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"{site.url}/pay.html")
            rec = Recorder(root=Path(__file__).resolve().parents[2], env={"HEAL_DIR": "."}, probe=vendored_probe())
            rec.pending = {}
            rec.record(page.frame_locator('iframe[name="payment"]').locator("input[name=card]"))
            (_, entry), = rec.pending.items()
        finally:
            browser.close()
    assert entry["fingerprint"]["frame"] == "payment"
    assert entry["page"] == "/pay.html"            # not "/pay-frame.html"


def test_a_framed_element_heals_after_a_forced_reopen_of_the_top_page(site, tmp_path):
    """The failing page is gone: the heal reopens the route the recording stored, which must be the TOP page's (one with the
    iframe), so the framed field is in what the service is shown. With the frame document's route it is not."""
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    from pytest_voyageur import fingerprints as fp
    from pytest_voyageur.client import HealSettings, Healer, LivePage, vendored_probe
    from tests.unit.test_client import FakeService, reply
    site.set("pay.html", PAY_PAGE)
    site.set("pay-frame.html", PAY_FRAME_DRIFTED)
    (tmp_path / "pages").mkdir()
    act = tmp_path / "pages" / "pay_page.py"
    act.write_text("def act(rec, loc):\n    rec.record(loc)\n")
    ns: dict = {}
    exec(compile(act.read_text(), str(act), "exec"), ns)
    env: dict = {}                                                       # the default .heal folder, as the Healer reads it
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"{site.url}/pay.html")
            rec = fp.Recorder(root=tmp_path, env=env, probe=vendored_probe())
            rec.pending = {}
            ns["act"](rec, page.frame_locator('iframe[name="payment"]').locator("#cc-number"))   # green run, before the drift
            fp.merge_into(tmp_path, {k: v for k, v in rec.pending.items() if v}, env)
        finally:
            browser.close()                                            # the failing page is gone from here on
    settings = HealSettings(url="http://svc", base_url=site.url)
    svc = FakeService(reply("needs_human", reason="x", why="stop"))
    failure = {"name": "t", "error": "TimeoutError: Locator.fill", "frames": [(str(act), 2)], "files": [str(act)]}
    healer = Healer(svc.client(), settings, lambda f: False, root=tmp_path, run_id="r1",
                    live_factory=lambda route, probe: LivePage(settings, route, probe))
    healer.heal(failure)
    (body,) = svc.posts("/v1/heal")
    assert body["page"]["url"] == "/pay.html"
    assert any(i.get("frame") == "payment" and i.get("testid") == "card-no" for i in body["page"]["inventory"])
