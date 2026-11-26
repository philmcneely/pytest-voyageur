"""
===============================================================================
Unit Tests: the selector guard and the frame chain rules (a service reply is untrusted input)
===============================================================================
A compromised service must not use a locator as a read oracle (`input[value^=s3]`, xpath, `:has-text`, a regex that backtracks
for ever) or smuggle a frame chain the client cannot run: the same rules as the TypeScript client's PROBING_SELECTOR, for
`locator` AND `frame_locator` string arguments, in the validator and in the locator builder. A chain may not end on a frame.

Author: PMAC
===============================================================================
"""
import pytest

from pytest_voyageur.client import build_locator, check_replacement, validate_replacement
from tests.unit.test_client import Recorder

ORIGINAL = 'page.locator("#a")'

PROBING = [
    "input[value^=s3]", "input[value$=s3]", "input[value*=s3]", "input[value~=s3]", "input[value|=s3]", "input[value=s3]",
    "input[name^=pay]", "input[name$=ay]", "input[name*=ay]", "[ data-x ^= 'a']", "xpath=//input", "//input", "(//input)[1]",
    "..", "text=/^(a+)+$/", "text=/a/", "css=input", "internal:text=x", "role=button", "input >> nth=0", "a >> b",
    "p:has-text('s3')", "p:text('s3')", "p:nth-match(input, 1)", "input[type=hidden]", "input[type='hidden']", "a\\76 b",
    "a /* c */", "input[\\76alue^=s]", "id=x",
]
SAFE = ["#a", "input[name=card]", 'iframe[name="payment"]', "tag[data-x='y']", "[data-testid=card-no]", ".row .cell"]


@pytest.mark.parametrize("sel", PROBING)
@pytest.mark.parametrize("method", ["locator", "frame_locator"])
def test_probing_selectors_are_refused_everywhere(method, sel):
    tail = ".locator('b')" if method == "frame_locator" else ""
    to = f"page.{method}({sel!r}){tail}"
    assert validate_replacement(to, ORIGINAL) is None
    assert check_replacement(to, ORIGINAL) is not None
    assert build_locator(Recorder(), to) is None


@pytest.mark.parametrize("sel", PROBING[:6])
def test_a_probing_selector_as_a_keyword_or_after_a_frame_hop_is_refused(sel):
    assert validate_replacement(f"page.frame_locator('#f').locator(selector={sel!r})", ORIGINAL) is None
    assert build_locator(Recorder(), f"page.frame_locator('#f').locator({sel!r})") is None


@pytest.mark.parametrize("sel", SAFE)
def test_plain_css_still_passes(sel):
    assert validate_replacement(f"page.locator({sel!r})", ORIGINAL) is not None
    assert build_locator(Recorder(), f"page.frame_locator('#f').locator({sel!r})") is not None


REFUSED = [
    'page.frame_locator("#f")', 'page.frame_locator("#f").first', 'page.frame_locator("#f").nth(0)',
    'page.frame_locator("#f").first()', 'page.frame_locator("#f").last()', 'page.frame_locator("#f").locator("b").first()',
    'page.frame_locator("#f").filter(has_text="x")', 'page.frame_locator("#f").first.filter(has_text="x")',
    'page.frame_locator("#f").nth(1).filter(has_text="x")',
    "page.frame_locator", "page.locator", 'page.locator("a").frame_locator', 'page.locator("a").first.frame_locator',
    'page.frame_locator(...).locator("c")', 'page.frame_locator(b"x").locator("c")', 'page.frame_locator(1j).locator("c")',
    'page.frame_locator(1).locator("c")', 'page.frame_locator(None).locator("c")', 'page.frame_locator(True).locator("c")',
    'page.frame_locator("a\\x00").locator("b")', 'page.frame_locator("a\\n").locator("b")',
    'page.frame_locator("a\\u202e").locator("b")', 'page.locator(b"x")', "page.locator(1j)", "page.locator(...)",
    'page.frame_locator("a").locator("b").__class__', 'page.frame_locator("a").owner', 'page.frame_locator("a").locator("b").page',
]


@pytest.mark.parametrize("to", REFUSED)
def test_frame_chain_and_argument_rules(to):
    assert validate_replacement(to, ORIGINAL) is None
    assert check_replacement(to, ORIGINAL) is not None


@pytest.mark.parametrize("to", [
    'page.frame_locator("#f").locator("b")', 'page.frame_locator("a").frame_locator("b").locator("c")',
    'page.frame_locator("a").first.locator("b")', 'page.frame_locator("a").nth(1).get_by_role("button", name="Go", exact=True)',
    'self.page.frame_locator("a").locator("b").first', 'page.locator("a").locator("b").nth(2)',
])
def test_legitimate_chains_still_pass(to):
    original = "self.page.locator('x')" if to.startswith("self") else ORIGINAL
    assert validate_replacement(to, original) == to
