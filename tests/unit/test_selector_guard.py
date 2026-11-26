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
# the reviewers' payloads: namespaced attribute names, hidden-input state, nested pseudo-classes that hang count(), engines, lists
REVIEWED = [
    "input[*|value=s3cret]", "input[|value=s3cret]", "input[ *|value = 's3cret' ]", "input[VALUE=s3cret]", "input[value]",
    "input[value='s3cret' i]", "input[type='HIDDEN']", "input[type=hidden i]", "input[type = hidden]", "input[type|=hidden]",
    "input:checked", "input:placeholder-shown", "input:not([type=text])", "input:is([type=hidden])", "form:has(input[name=csrf])",
    "p:has(+ p)", ":is(p,span):nth-child(1)", '"token: abc123"', "'token'", "/(a+)+$/", "p:text-is('x')", "p:TEXT('x')", "p:HAS-TEXT('x')",
    "p:right-of(input)", "p:below(input)", "p:visible", "p:scope", " xpath=//p", "\txpath=//p", "data-testid=x", "p>>nth=0",
    "body > *", "*", "input[name=csrf]:not([value=zz])", "input[name='a'][value='b']", "input[ value = x ]", "input[\nvalue=x]",
    "p::after", "p:lang(en)", "p:nth-child(2n+1 of p)", "p:nth-last-of-type(999999999999)", "input:read-only", "input:valid",
    "p:not(:has(:is(p:has(p))))", ":is(" * 15 + "p" + ")" * 15, ":is(" * 40 + "p" + ")" * 40, "p:has(" * 40 + "p" + ")" * 40,
    ", ".join(["p"] * 50), "p, span", "p:not(.a)" * 10, "[data-x='" + "a" * 100000 + "']", "p " * 100, "p > " * 100 + "p",
    "div:nth-child(odd)", "a:hover", "input[name=a b]", "input[name=\"a]", "input[name=\"a\\\"]", "#a#b#", ".a..b", "#1a", ".1a",
    "[data-x=\"a\"" , "[=x]", "[a=]", "[a==b]", "[a='b'c]", "p >", "> p", "p > > p", "p  >  >p",
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


@pytest.mark.parametrize("sel", REVIEWED)
@pytest.mark.parametrize("method", ["locator", "frame_locator"])
def test_every_reviewed_payload_is_refused_by_the_allowlist(method, sel):
    to = f"page.{method}({sel!r})" + (".locator('b')" if method == "frame_locator" else "")
    assert validate_replacement(to, ORIGINAL) is None
    assert build_locator(Recorder(), to) is None


@pytest.mark.parametrize("sel", [
    "#a", "#card-number", "input", "input[name=card]", 'input[name="card"]', "input[name='card']", "[data-testid=card-no]",
    'input[data-qa="a b"]', 'label[for="x"]', "button.btn.primary", 'input[role="textbox"].field', 'iframe[name="payment"]',
    'iframe[title="Secure card"]', '[id="a:b"]', "[data-cy='it\\'s']", ".row .cell", 'tr[data-row="7"] > td.name', "div > ul li a",
    "input[type=email]", 'input[type="text"]',
])
@pytest.mark.parametrize("method", ["locator", "frame_locator"])
def test_the_shapes_the_service_writes_are_accepted(method, sel):
    assert build_locator(Recorder(), f"page.{method}({sel!r})" + (".locator('b')" if method == "frame_locator" else "")) is not None


def test_nothing_nested_and_no_long_selector_by_construction():
    from pytest_voyageur.client import allowed_selector
    assert not allowed_selector("a b c d e")                 # more than four compounds
    assert not allowed_selector("x" * 501)
    assert not allowed_selector("a(b)") and not allowed_selector(":is(a)") and not allowed_selector("a,b")


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


# ---- the original is a person's code: only the replacement is held to the grammar ------------------------------------------
@pytest.mark.parametrize("original", [
    'page.locator("div.row:nth-child(2) button")', 'page.locator("a, b")', 'page.locator("//div")', 'page.locator("input[name^=a]")',
    'page.locator("xpath=//div")', 'page.locator("[id$=\'x\']")', 'page.frame_locator("iframe[name^=Apx]").get_by_role("textbox", name="Card")',
    'page.frame_locator("iframe[name^=Apx]").locator("input[name^=card]")',
])
def test_an_original_with_ordinary_css_can_still_be_healed(original):
    assert check_replacement('page.locator("#a")', original) is None
    assert check_replacement('page.frame_locator("#f").locator("#a")', original) is None


# ---- [id$="tail"] is written by the service for generated ids: a suffix match on `id` only ---------------------------------
@pytest.mark.parametrize("sel", ['[id$="tail"]', "[id$=tail]", "button[id$='save']", "div > [id$=\"x\"]"])
def test_a_suffix_match_on_id_is_accepted(sel):
    assert validate_replacement(f"page.locator({sel!r})", ORIGINAL) is not None
    assert build_locator(Recorder(), f"page.locator({sel!r})") is not None


@pytest.mark.parametrize("sel", [
    "[name$=x]", "[data-a$=x]", "[value$=x]", "[ID^=x]", "[id^=x]", "[id*=x]", "[id~=x]", "[id|=x]", "[idx$=x]", "[type$=hidden]",
    "[Name$=x]", "[class$=x]", "[id$=x i]", "[id $=x]", "[id$ =x]",
])
def test_a_suffix_match_on_anything_but_id_is_refused(sel):
    assert validate_replacement(f"page.locator({sel!r})", ORIGINAL) is None
    assert build_locator(Recorder(), f"page.locator({sel!r})") is None


# ---- combinators are an ASCII space or `>` and nothing else -------------------------------------------------------------
@pytest.mark.parametrize("sel", [
    "a\tb", "a\nb", "a\rb", "a\r\nb", "a\u00a0b", "a\u3000b", "a\u2028b", "a\u2003b", "a\u0085b", "a\x0bb", "a\x0cb",
    "a\t>\tb", "a\u00a0>\u00a0b", "a>\u3000b", "a\u2028>b", "a\n> b", "a >\nb",
])
def test_only_an_ascii_space_or_a_chevron_joins_compounds(sel):
    assert validate_replacement(f"page.locator({sel!r})", ORIGINAL) is None
    assert build_locator(Recorder(), f"page.locator({sel!r})") is None


@pytest.mark.parametrize("sel", ["a b", "a  b", "a>b", "a > b", "a >b", "a> b", "a   >   b"])
def test_ascii_spaces_and_chevrons_still_join(sel):
    assert validate_replacement(f"page.locator({sel!r})", ORIGINAL) is not None
