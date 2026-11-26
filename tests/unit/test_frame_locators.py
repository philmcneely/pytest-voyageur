"""
===============================================================================
Unit Tests: frame_locator hops in a heal replacement
===============================================================================
The service writes `page.frame_locator("iframe[name=payment]").locator("input[name=card]")` for an element inside an iframe.
The client's validators must accept a frame_locator hop as a root call and anywhere in a chain (nested frames included), and
keep refusing every non-literal argument, unknown method and foreign receiver.

Author: PMAC
===============================================================================
"""
import pytest

from pytest_voyageur.client import build_locator, check_replacement, validate_replacement
from tests.unit.test_client import Recorder

ORIGINAL = "page.frame_locator('iframe[name=\"payment\"]').locator('input[name=card-old]')"

OK = [
    "page.frame_locator('iframe[name=\"payment\"]').locator('input[name=card]')",
    "self.page.frame_locator('#pay').get_by_role('textbox', name='Card number')",
    "page.frame_locator('#a').frame_locator('#b').get_by_label('Card number')",
    "page.frame_locator('#a').frame_locator('#b').locator('#c').first",
    "page.locator('#row').frame_locator('#b').locator('input')",
    "page.frame_locator('#a').first.locator('input')",
    "page.frame_locator('#a').nth(1).locator('input')",
]
BAD = [
    "page.frame_locator(sel).locator('input')",
    "page.frame_locator('#a' + x).locator('input')",
    "page.frame_locator('#a').locator(x)",
    "page.frame_locator('#a').evaluate('1')",
    "page.frame_locator('#a').locator('input').click()",
    "page.frame_locator('#a').frame('x').locator('input')",
    "page.frame_locator('#a').owner.locator('input')",
    "other.frame_locator('#a').locator('input')",
    "page.frame_locator('#a'); x()",
    "page.frame_locator('#a').locator('input')  # c",
    "page.frame_locator()",
    "page.frame_locator('#a')",
    "page.frame_locator('#a').first",
    "page.frame_locator('#a').nth(1)",
]


@pytest.mark.parametrize("to", OK)
def test_frame_chains_are_accepted(to):
    original = "page.frame_locator('#a').locator('x')" if to.startswith("page") else "self.page.frame_locator('#a').locator('x')"
    assert validate_replacement(to, original) == to
    assert check_replacement(to, original) is None


@pytest.mark.parametrize("to", BAD)
def test_hostile_frame_chains_are_refused(to):
    assert validate_replacement(to, ORIGINAL) is None
    assert check_replacement(to, ORIGINAL) is not None


def test_a_frame_root_must_keep_the_receiver():
    assert validate_replacement("self.page.frame_locator('#a').locator('i')", ORIGINAL) is None


def test_a_page_locator_original_can_be_healed_into_a_frame_chain():
    assert check_replacement("page.frame_locator('#a').locator('i')", "page.locator('#old')") is None


def test_build_walks_a_nested_frame_chain():
    loc = build_locator(Recorder(), "page.frame_locator('#a').frame_locator('#b').get_by_label('Card')")
    assert loc.steps == (("frame_locator", ("#a",), {}), ("frame_locator", ("#b",), {}), ("get_by_label", ("Card",), {}))


def test_build_refuses_a_non_literal_frame_selector():
    assert build_locator(Recorder(), "page.frame_locator(x).locator('i')") is None
