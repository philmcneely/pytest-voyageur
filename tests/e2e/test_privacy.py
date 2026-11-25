"""
===============================================================================
Privacy in a Real Browser (what a person typed never reaches the service)
===============================================================================
The failing test's own page is probed, so whatever the test typed before it failed is on that page. This runs the client's
snapshot against a real Chromium page holding every kind of typed or chosen text and holds the request body to one rule: none
of it is in what would be sent.

Features:
    ✓ Typed text in a textbox, a password field, a textarea, a select, an editable region and a search field is not in the body
    ✓ A link's query and fragment are not in the body
    ✓ The inventory holds no key named like a value

Environment Variables:
    None.

Author: PMAC
===============================================================================
"""
import json

import pytest

from pytest_voyageur.client import AttachedPage, HealSettings, build_request, vendored_probe
from tests.e2e.conftest import needs_chromium

pytestmark = [pytest.mark.browser, needs_chromium]

HTML = """<html lang="en"><body>
<form>
 <input aria-label="Name" id="n"><input type="password" aria-label="Pw" id="p"><input type="search" aria-label="Find" id="s">
 <textarea aria-label="Notes" id="t"></textarea>
 <select aria-label="Plan" id="sel"><option>free</option><option>CHOSEN-plan-text</option></select>
 <div contenteditable="true" aria-label="Rich" id="rich"></div>
</form>
<a href="/reset?token=URL-SECRET-123&u=zzq77#frag-secret">Reset</a><button id="go">Go</button>
</body></html>"""
TYPED = ["TYPED-name-111", "TYPED-pw-222", "TYPED-find-333", "TYPED-notes-444", "CHOSEN-plan-text", "TYPED-rich-555"]


def test_nothing_typed_or_chosen_is_in_the_request(page):
    page.set_content(HTML)
    page.locator("#n").fill(TYPED[0])
    page.locator("#p").fill(TYPED[1])
    page.locator("#s").fill(TYPED[2])
    page.locator("#t").fill(TYPED[3])
    page.locator("#sel").select_option(label=TYPED[4])
    page.locator("#rich").evaluate("(el, v) => { el.textContent = v; }", TYPED[5])

    view = AttachedPage(HealSettings(base_url="http://app.example"), page, vendored_probe())
    body = build_request({"name": "t", "nodeid": "tests/t.py::t", "error": "boom"}, [], [], view.snapshot(), view.settings,
                         __import__("pathlib").Path("."))
    sent = json.dumps(body)

    for secret in TYPED + ["URL-SECRET-123", "frag-secret", "zzq77"]:
        assert secret not in sent, secret
    assert "Rich" in sent and "Reset" in sent                           # the page is still described
    assert not [k for item in body["page"]["inventory"] for k in item if "value" in k.lower()]
