"""
===============================================================================
Heal Service Client (evidence out, patch in, nothing about models)
===============================================================================
Talks to a Voyageur service (HEAL_SERVICE_URL) on behalf of the pytest plugin (plugin.py). It knows HTTP, the browser, source
files and its own safety checks, and nothing about models: no model URL, no model name, no prompt and no parsing of a model
reply live in this package. The service picks the element and writes the locator; this client only
    - builds the request: the stack files under the source roots (capped, with sha256), the probe's element inventory of the
      failing page, the aria snapshot, the language signals, the last green run's fingerprint and its neighbours
    - checks every candidate in the live page BEFORE touching a file: the locator must match exactly one element and that
      element must be the one the service expects (the probe's `only` mode describes it; no code from the service is ever
      evaluated, the locator expression is walked and only approved Playwright calls with literal arguments are run)
    - applies a patch only when its base hash is the file's current hash, the file is one the client sent and sits under the
      source roots, the span holds exactly `from`, and `to` passes the locator validator (each language branch separately)
    - re-runs the exact test, keeps the edit only when the service confirms `healed`, reverts it otherwise, and reports every
      try to the service so it can move to the next candidate

Features:
    ✓ HealSettings: HEAL_SERVICE_URL, HEAL_SERVICE_TOKEN, HEAL_VISION (accepted; this client never sends a screenshot),
      HEAL_MASK (accepted, unused until vision), HEAL_TEST_ID_ATTRIBUTE, HEAL_SOURCE_ROOTS, HEAL_STORAGE_STATE, BASE_URL
    ✓ ServiceClient: capabilities, probe.js (the vendored copy), heal, result, audit; every failure is a HealServiceError that
      names the status and the service's reason, never the request
    ✓ Language signals: <html lang>, the browser locale and the Content-Language header of the page load
    ✓ Patch safety refusals: stale base hash, file outside the source roots or never sent, span that does not hold `from`,
      overlapping spans, a replacement that fails validate_replacement, a branch set that does not match the code
    ✓ A service or browser error ends that heal as needs_human with the reason; it never crashes the run
    ✓ Language branches: the by_language helper (pytest_voyageur.language) is imported into a file that receives branch code
    ✓ Input values are never sent: the aria snapshot goes through redact.redact_typed_values and the inventory is cut to an
      allowlist of keys on THIS side, whatever the service or its probe promises; no cookie, storage state or screenshot
      leaves the client
    ✓ Every service reply is untrusted: a replacement is one expression with no comment and no control character whose
      unparse round-trips; the original span must be a pure locator chain AND exactly one expression node of the file on
      disk; the code written is the code checked live (every also-patch is checked live too); a file is written in place
      with its mode, never through a symlink and never outside its source root; a partial multi-file write is reverted
    ✓ The probe is vendored (probe.js, sha256 in probe.sha256) and runs with page requests to origins other than the app's
      blocked; the service's copy is used only with HEAL_PROBE_FROM_SERVICE=1 and only when its sha256 equals the vendored
    ✓ A bearer token is never sent over http:// to a host other than localhost unless HEAL_ALLOW_INSECURE=1
    ✓ Every try is recorded (locator, live count, hit, test outcome, kept or reverted) and returned with the decision

Environment Variables:
    HEAL_SERVICE_URL: base URL of the Voyageur service (the plugin defaults to http://127.0.0.1:8765)
    HEAL_SERVICE_TOKEN: bearer token, when the service requires one
    HEAL_VISION: off | auto | on (accepted; screenshots are not captured by this client, so it is always off)
    HEAL_MASK: locators to mask in a screenshot (accepted, unused until vision)
    HEAL_TEST_ID_ATTRIBUTE: the project's test id attribute (default data-testid)
    HEAL_SOURCE_ROOTS: comma separated source roots, relative paths inside the repository ("." is the whole repository);
      default pages,tests,utils plus the top directory of every collected test file
    HEAL_PROBE_FROM_SERVICE: "1" to fetch the probe from the service (used only when it is byte-identical to the vendored one)
    HEAL_ALLOW_INSECURE: "1" to send the bearer token over http:// to a host other than localhost
    HEAL_STORAGE_STATE: Playwright storage-state file used to reopen a page as a signed-in user (never sent)
    HEAL_REPO: a name for the repository, sent as run.repo (optional)
    BASE_URL: the app under test, used to reopen the failing route when the test's own page cannot be used

Author: PMAC
===============================================================================
"""
import ast
import hashlib
import json
import os
import io
import re
import signal
import stat
import threading
import time
import tokenize
import unicodedata
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from . import __version__
from .fingerprints import (NEVER_SOURCE, call_key, heal_dir, last_pass_for, line_of, load_store, relative_under,
                           source_roots, under_root)
from .language import page_language
from .redact import clean_inventory, redact_typed_values, redact_urls, scrub_values

LIMITS = {"files": 20, "file_bytes": 256 * 1024, "aria_chars": 256 * 1024, "error_chars": 64 * 1024, "error_line": 8 * 1024,
          "expression": 4000}
MAX_TRIES = 5                    # candidates one heal may try (the service is asked for 3 rounds; it may not ask for more than 5)
TIME_BUDGET = 900.0              # seconds one heal may run before it stops asking for another candidate (HEAL_TIME_BUDGET)
_HEAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_URL_SECRET = re.compile(r"(https?://[^\s\"'<>?#]*)[?#][^\s\"'<>]*")
HELPER = "by_language"
HELPER_MODULE = "pytest_voyageur.language"
HELPER_IMPORT = f"from {HELPER_MODULE} import by_language"
HELPER_MODULES = {HELPER_MODULE, "utils.heal_locale"}     # the second is the framework's copy, which a file may already import

# A replacement may only be a chain of these Playwright locator calls on the same receiver as the expression it replaces,
# with literal arguments.
ALLOWED_LOCATOR_METHODS = {
    "locator", "get_by_role", "get_by_label", "get_by_text", "get_by_placeholder",
    "get_by_test_id", "get_by_alt_text", "get_by_title", "first", "last", "nth", "filter",
    "frame_locator",                                  # a hop into an iframe: root call or anywhere in the chain
}
_LANG_KEY = re.compile(r"^[a-z]{2,3}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_NETWORK_SCHEMES = {"http", "https", "ws", "wss"}
_BAD_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}              # control, format (bidi), line and paragraph separators
PROBE_FILE = Path(__file__).with_name("probe.js")
DEFAULT_ROOTS = ("pages", "tests", "utils")
_TRACE_LINE = re.compile(r"^[ \t]*(\S+?\.py):(\d+)\b", re.M)


# ---- settings --------------------------------------------------------------------------------------------------------
@dataclass
class HealSettings:
    """What the client reads from the environment."""
    url: str = ""
    token: str = ""
    vision: str = "off"
    mask: List[str] = field(default_factory=list)
    test_id_attribute: str = "data-testid"
    roots: Tuple[str, ...] = DEFAULT_ROOTS
    storage_state: Optional[str] = None
    base_url: str = ""

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None, extra_roots: Tuple[str, ...] = ()) -> "HealSettings":
        """`extra_roots` are the directories of the collected tests, used when HEAL_SOURCE_ROOTS names none."""
        env = env if env is not None else os.environ
        attr = env.get("HEAL_TEST_ID_ATTRIBUTE", "data-testid").strip().lower()
        return cls(url=env.get("HEAL_SERVICE_URL", "").strip().rstrip("/"), token=env.get("HEAL_SERVICE_TOKEN", "").strip(),
                   vision=env.get("HEAL_VISION", "off").strip().lower() or "off",
                   mask=[m.strip() for m in env.get("HEAL_MASK", "").split(",") if m.strip()],
                   test_id_attribute=attr if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", attr) else "data-testid",
                   roots=source_roots(env, DEFAULT_ROOTS, extra_roots),
                   storage_state=env.get("HEAL_STORAGE_STATE") or None, base_url=env.get("BASE_URL", "").strip())


# ---- the service -----------------------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def vendored_probe() -> str:
    """The page probe shipped in this package (a copy of the service's, never fetched from it by default)."""
    return PROBE_FILE.read_text(encoding="utf-8")


class HealServiceError(Exception):
    """The service could not be reached or refused a call. The message names the status and reason, never the request."""


class ServiceClient:
    """The service's HTTP API. `http` may be any httpx.Client (a test passes one on a MockTransport or an ASGI app)."""

    def __init__(self, base_url: str, token: str = "", http: Optional[httpx.Client] = None, timeout: float = 200.0,
                 env: Optional[Dict[str, str]] = None):
        self.base = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=timeout)
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.env = env if env is not None else os.environ
        self._probe: Optional[str] = None
        parts = urlsplit(self.base)
        self._insecure = bool(token) and parts.scheme == "http" and (parts.hostname or "") not in _LOCAL_HOSTS \
            and self.env.get("HEAL_ALLOW_INSECURE") != "1"
        if self._insecure:
            warnings.warn("HEAL_SERVICE_TOKEN would travel over plain http:// to a host other than localhost; the heal "
                          "client refuses to send it (use https://, or set HEAL_ALLOW_INSECURE=1 on a trusted network)",
                          UserWarning, stacklevel=2)

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> httpx.Response:
        if self._insecure:
            raise HealServiceError("refusing to send the bearer token over http:// to a host other than localhost "
                                   "(use https://, or set HEAL_ALLOW_INSECURE=1)")
        try:
            # the service refuses a POST that is not JSON by header (a web page cannot send that without a CORS preflight)
            headers = {**self.headers, "Content-Type": "application/json"} if body is not None else self.headers
            resp = self.http.request(method, self.base + path, headers=headers,
                                     content=json.dumps(body).encode() if body is not None else None)
        except httpx.HTTPError as e:
            raise HealServiceError(f"the heal service could not be reached ({type(e).__name__})") from None
        if resp.status_code >= 400:
            try:
                body_json = resp.json()
                detail = body_json.get("detail") if isinstance(body_json, dict) else None
            except (ValueError, RecursionError, MemoryError):
                detail = None
            raise HealServiceError(f"the heal service answered {resp.status_code}: " + str(detail or resp.reason_phrase)[:300])
        return resp

    def _json(self, resp: httpx.Response) -> Dict[str, Any]:
        try:
            data = resp.json()
        except (ValueError, RecursionError, MemoryError):
            # An untrusted service can nest JSON deep enough to blow the parser's stack; that is a bad reply, not a crash.
            raise HealServiceError("the heal service sent a reply that is not usable JSON") from None
        if not isinstance(data, dict):
            raise HealServiceError("the heal service sent a reply that is not a JSON object")
        return data

    def capabilities(self) -> Dict[str, Any]:
        return self._json(self._call("GET", "/v1/capabilities"))

    def probe_js(self) -> str:
        """The in-page probe script: the vendored copy (probe.js). With HEAL_PROBE_FROM_SERVICE=1 the service's
        copy is fetched and checked byte for byte against it (sha256); a different script is refused, never run."""
        if self._probe is None:
            vendored = vendored_probe()
            if self.env.get("HEAL_PROBE_FROM_SERVICE") == "1":
                served = self._call("GET", "/v1/probe.js").content
                if _sha(served) != _sha(vendored.encode("utf-8")):
                    raise HealServiceError("the service's probe script differs from the vendored one (sha256), so it is "
                                           "refused")
            self._probe = vendored
        return self._probe

    def heal(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self._json(self._call("POST", "/v1/heal", request))

    def result(self, heal_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(heal_id, str) or not _HEAL_ID.fullmatch(heal_id):
            raise HealServiceError("the heal service sent an id that cannot be used in a request path")
        return self._json(self._call("POST", f"/v1/heals/{heal_id}/result", report))

    def audit(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self._json(self._call("POST", "/v1/audit", request))


# ---- the locator validator (a second check on everything the service sends) -------------------------------------------
def _has_control(text: str) -> bool:
    """True when `text` holds a control character (a carriage return, form feed, tab, NUL, C1), a bidi or other format
    character, or a line or paragraph separator: anything that can hide or end a line differently from how it reads."""
    return any(unicodedata.category(c) in _BAD_CATEGORIES for c in text)


def _token_values(text: str) -> Optional[List[Tuple[str, Any]]]:
    """The tokens of `text` that carry meaning (strings by value, so quote style does not matter), or None when it holds a
    comment, cannot be tokenized, or has an error token."""
    out: List[Tuple[str, Any]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT or tok.type == tokenize.ERRORTOKEN:
                return None
            if tok.type in (tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER, tokenize.INDENT, tokenize.DEDENT):
                continue
            if tok.type == tokenize.STRING:
                try:
                    out.append(("string", ast.literal_eval(tok.string)))
                    continue
                except (ValueError, SyntaxError):
                    return None
            out.append((tokenize.tok_name[tok.type], tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return out


def _single_expression(text: Any) -> Optional[ast.expr]:
    """The expression `text` is, when it is exactly one on one line with no comment and no control character, and its
    ast.unparse round-trips (the same tokens once quote style and spacing are normalized). Otherwise None."""
    if not isinstance(text, str) or not text.strip() or len(text) > LIMITS["expression"] or "\n" in text or ";" in text \
            or _has_control(text):
        return None
    try:
        tree = ast.parse(text, mode="eval")
        normal = ast.unparse(tree)
        if ast.unparse(ast.parse(normal, mode="eval")) != normal:
            return None
    except (SyntaxError, ValueError, RecursionError):
        return None
    given = _token_values(text)
    if given is None or given != _token_values(normal):
        return None
    return tree.body


def _normal(text: Any) -> Optional[str]:
    """`text` in its normalized form (ast.unparse), or None when it is not a single clean expression."""
    tree = _single_expression(text)
    return ast.unparse(tree) if tree is not None else None


def validate_replacement(proposed, original):
    """
    Return the replacement if it is a safe drop-in for ``original``, else None.
    Must be a single-line expression, with no comment and no control character, that is a chain of approved Playwright
    locator calls with literal arguments, rooted at the same receiver (``page`` / ``self.page``) as the original.
    """
    tree = _single_expression(proposed)
    if tree is None or not isinstance(original, str):
        return None
    try:
        original_tree = ast.parse(original, mode="eval").body
    except (SyntaxError, ValueError):
        return None

    def receiver(node):
        while isinstance(node, (ast.Call, ast.Attribute)):
            node = node.func if isinstance(node, ast.Call) else node.value
        return node

    def base_receiver(node):
        # the object the first locator method is called on, e.g. self.page
        chain = []
        while isinstance(node, (ast.Call, ast.Attribute)):
            chain.append(node)
            node = node.func if isinstance(node, ast.Call) else node.value
        for n in reversed(chain):
            if isinstance(n, ast.Attribute) and n.attr in ALLOWED_LOCATOR_METHODS:
                return ast.unparse(n.value)
        return None

    if not isinstance(tree, (ast.Call, ast.Attribute)) or not isinstance(receiver(tree), ast.Name):
        return None
    if base_receiver(tree) != base_receiver(original_tree):
        return None
    end = tree
    while True:                                        # a frame on its own (even `.first` / `.nth(1)` of one) names no element
        if isinstance(end, ast.Attribute) and end.attr in ("first", "last"):
            end = end.value
        elif isinstance(end, ast.Call) and isinstance(end.func, ast.Attribute) and end.func.attr in ("nth", "frame_locator"):
            if end.func.attr == "frame_locator":
                return None
            end = end.func.value
        else:
            break
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            # attribute access is allowed only for approved methods and the receiver chain
            if node.attr not in ALLOWED_LOCATOR_METHODS and ast.unparse(node) not in {"self.page"}:
                return None
        elif isinstance(node, ast.Call):
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                if not isinstance(arg, ast.Constant):
                    return None
        elif not isinstance(node, (ast.Name, ast.Constant, ast.Load, ast.keyword)):
            return None
    return proposed


def _receiver_text(expr: str) -> Optional[str]:
    """The object the first locator method of `expr` is called on (`self.page`), or None."""
    try:
        node: Any = ast.parse(expr, mode="eval").body
    except SyntaxError:
        return None
    chain = []
    while isinstance(node, (ast.Call, ast.Attribute)):
        chain.append(node)
        node = node.func if isinstance(node, ast.Call) else node.value
    for n in reversed(chain):
        if isinstance(n, ast.Attribute) and n.attr in ALLOWED_LOCATOR_METHODS:
            return ast.unparse(n.value)
    return None


def _branch_dict(code: str) -> Optional[Tuple[str, Dict[str, str]]]:
    """(receiver, {language: code}) when `code` is `by_language(<receiver>, {"en": ..., "fr": ...})`, else None."""
    try:
        node = ast.parse(code, mode="eval").body
    except SyntaxError:
        return None
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == HELPER
            and len(node.args) == 2 and not node.keywords and isinstance(node.args[1], ast.Dict)):
        return None
    out: Dict[str, str] = {}
    for key, value in zip(node.args[1].keys, node.args[1].values):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str) and _LANG_KEY.match(key.value)) \
                or key.value in out:
            return None
        out[key.value] = ast.unparse(value)
    return ast.unparse(node.args[0]), out


def check_replacement(to: str, original: str, branches: Optional[Dict[str, str]] = None,
                      exact_branches: bool = True) -> Optional[str]:
    """None when `to` may replace `original`, else the reason it may not.

    A plain locator must pass validate_replacement. Branch code (by_language(receiver, {...})) must name the same languages
    as `branches`, carry the same locator for each (`exact_branches`; a patch in another file has its own receiver), and
    EVERY branch must pass validate_replacement on its own."""
    if validate_replacement(original, original) is None:
        return "the expression to replace is not a plain locator chain"
    if to == original:
        return "the replacement is identical to the failing locator"
    if _single_expression(to) is None:
        return "the replacement is not a single clean expression (no comment, control character or second statement)"
    if HELPER + "(" in to or branches:
        parsed = _branch_dict(to)
        if parsed is None or not branches:
            return "the replacement is not a by_language(receiver, {...}) call matching the candidate's branches"
        receiver, found = parsed
        if set(found) != set(branches):
            return "the languages in the code differ from the candidate's branches"
        if receiver != _receiver_text(original):
            return "the helper's receiver is not the receiver of the failing locator"
        for lang, code in found.items():
            if exact_branches and ast.unparse(ast.parse(branches[lang], mode="eval").body) != code:
                return f"the {lang} branch in the code differs from the candidate's branch"
            if validate_replacement(code, original) is None:
                return f"the {lang} branch is not a safe locator"
        return None
    return None if validate_replacement(to, original) is not None else "the replacement is not a safe locator expression"


def build_locator(page: Any, expression: str) -> Any:
    """The Playwright locator an approved expression stands for, built by walking its syntax tree: only the allowed
    locator methods, literal arguments, on `page` or `self.page`. Nothing is ever eval'd. None when it cannot be built."""
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return None

    def walk(n: Any) -> Any:
        if isinstance(n, ast.Call):
            f = n.func
            if not (isinstance(f, ast.Attribute) and f.attr in ALLOWED_LOCATOR_METHODS):
                raise ValueError("unsupported call")
            args = [a.value for a in n.args if isinstance(a, ast.Constant)]
            kwargs = {k.arg: k.value.value for k in n.keywords if k.arg and isinstance(k.value, ast.Constant)}
            if len(args) != len(n.args) or len(kwargs) != len(n.keywords):
                raise ValueError("non-literal argument")
            return getattr(walk(f.value), f.attr)(*args, **kwargs)
        if isinstance(n, ast.Attribute):
            if n.attr in ("first", "last"):
                return getattr(walk(n.value), n.attr)
            if ast.unparse(n) == "self.page":
                return page
            raise ValueError("unsupported attribute")
        if isinstance(n, ast.Name) and n.id == "page":
            return page
        raise ValueError("unsupported receiver")

    try:
        return walk(node)
    except (ValueError, AttributeError, TypeError):
        return None


# ---- the failure: stack, test id, source files, route ------------------------------------------------------------------
def stack_from_error(error: str, roots: Tuple[str, ...], root: Path) -> List[Dict[str, Any]]:
    """The traceback's frames under the source roots, innermost first: [{file, line}]. pytest prints outermost first."""
    seen, frames = set(), []
    for m in _TRACE_LINE.finditer((error or "")[:LIMITS["error_chars"]]):
        rel = relative_under(m.group(1), root, roots)
        if rel and (rel, int(m.group(2))) not in seen:
            seen.add((rel, int(m.group(2))))
            frames.append({"file": rel, "line": int(m.group(2))})
    return frames[::-1]


def stack_from_frames(frames: List[Tuple[str, int]], roots: Tuple[str, ...], root: Path) -> List[Dict[str, Any]]:
    """[{file, line}] innermost first from (path, line) pairs listed outermost first (a pytest traceback), keeping the ones
    under the source roots."""
    seen, out = set(), []
    for path, line in reversed(frames):
        rel = relative_under(str(path), root, roots)
        if rel and (rel, line) not in seen:
            seen.add((rel, line))
            out.append({"file": rel, "line": line})
    return out


def junit_test_id(classname: str, name: str, root: Path) -> str:
    """`tests/login/test_login.py::TestX::test_name` from a JUnit classname (dotted module path + classes) and name."""
    parts = (classname or "").split(".")
    for cut in range(len(parts), 0, -1):
        rel = "/".join(parts[:cut]) + ".py"
        if (root / rel).is_file():
            return "::".join([rel, *parts[cut:], name])
    return f"{classname}::{name}" if classname else name


def bounded_error(error: str) -> str:
    """The error text within the service's limits (64 KB in all, 8 KB a line): longer lines and a longer text are cut. The query
    and fragment of every URL in it are dropped (a link can carry a token or a code; the path is what matters)."""
    text = _URL_SECRET.sub(lambda m: m.group(1), (error or "")[:4 * LIMITS["error_chars"]])
    lines = [ln if len(ln) <= LIMITS["error_line"] else ln[:LIMITS["error_line"]] for ln in text.split("\n")]
    return "\n".join(lines)[:LIMITS["error_chars"]]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text_file(root: Path, rel: str) -> Optional[Tuple[str, bytes]]:
    """(text, raw bytes) of a file inside `root`, newlines untouched, or None when it is missing or not UTF-8."""
    try:
        raw = (root / rel).read_bytes()
        return raw.decode("utf-8"), raw
    except (OSError, UnicodeDecodeError):
        return None


def within_roots(root: Path, rel: str, roots: Tuple[str, ...]) -> bool:
    """True when `rel` is listed under a source root AND its real path (every symlink followed) is inside that root's real
    path, which itself must be inside the repository. A symlink to another root, to the repo's top level or out of the
    repository does not count."""
    try:
        base = root.resolve()
        real = (root / rel).resolve()
        for r in roots:
            if under_root(rel, r):
                top = (root / r).resolve()
                try:
                    top.relative_to(base)
                    real.relative_to(top)
                    shown = real.relative_to(base)
                    if set(shown.parts) & NEVER_SOURCE:                        # a symlink into .git, .venv, .heal ...
                        return False
                    if r == "." and shown.as_posix() != rel:                   # the whole repository is the root: no redirects
                        return False
                    return True
                except ValueError:
                    continue
    except OSError:
        pass
    return False


def collect_source(stack: List[Dict[str, Any]], roots: Tuple[str, ...], root: Path,
                   extra: Tuple[str, ...] = ()) -> List[Dict[str, str]]:
    """The files named in the failing stack (then `extra`, the test's own file when the stack names none) that sit under the
    source roots, as {path, sha256, content}. At most 20 files, 256 KB each; a file whose real path (symlinks followed) is not
    inside the source root it is listed under is skipped."""
    files: List[Dict[str, str]] = []
    for rel in dict.fromkeys([*(f["file"] for f in stack), *extra]):
        if len(files) >= LIMITS["files"] or not rel.endswith(".py") or not within_roots(root, rel, roots):
            continue
        got = read_text_file(root, rel)
        if got and len(got[1]) <= LIMITS["file_bytes"]:
            files.append({"path": rel, "sha256": _sha(got[1]), "content": got[0]})
    return files


def route_from_source(content: str) -> str:
    """The page route a page object's `self.url = f"{BASE_URL}/path"` names; "/" when it names none."""
    m = re.search(r'self\.url\s*=\s*f?["\'][^"\']*BASE_URL\}?([^"\']*)["\']', content)
    return m.group(1) if m else "/"


def page_section(page: Dict[str, Any]) -> Dict[str, Any]:
    """The request's `page` object from a PageView snapshot. No screenshot is ever included (vision is off), and no input
    value: the inventory is cut to the allowlist and the aria snapshot has its typed text removed, on this side."""
    out: Dict[str, Any] = {
        "url": page["path"], "language": page["language"], "inventory": clean_inventory(page["inventory"]),
        "inventory_more": page["more"], "inventory_copy_more": page.get("copy_more", 0),
        "inventory_copy_withheld": page.get("copy_withheld", 0),
        "inventory_copy_withheld_data": page.get("copy_withheld_data", 0), "viewport": page["viewport"], "screenshot": None}
    aria = redact_urls(redact_typed_values(page["aria"])) if isinstance(page.get("aria"), str) else ""
    if aria and len(aria) <= LIMITS["aria_chars"]:
        out["aria_snapshot"] = aria
    return out


def build_request(failure: Dict[str, Any], files: List[Dict[str, str]], stack: List[Dict[str, Any]], page: Dict[str, Any],
                  settings: HealSettings, root: Path, last_pass: Optional[Dict[str, Any]] = None,
                  run_id: Optional[str] = None) -> Dict[str, Any]:
    """The POST /v1/heal body. `page` is a PageView snapshot: path, language signals, inventory, aria, viewport."""
    test_id = failure.get("nodeid") or junit_test_id(failure.get("classname", ""), failure["name"], root)
    req: Dict[str, Any] = {
        "client": {"framework": "pytest-voyageur", "language": "python", "version": __version__},
        "test": {"id": test_id[:500], "title": failure["name"][:500]},
        "failure": {"error": bounded_error(failure.get("error") or ""), "stack": stack},
        "source": {"roots": list(settings.roots), "files": files},
        "page": page_section(page),
        "options": {"vision": "off", "max_rounds": 3, "locale_branching": True},
        "test_id_attribute": settings.test_id_attribute,
        "run": {"id": run_id or uuid.uuid4().hex[:12]},
    }
    if last_pass:
        req["last_pass"] = last_pass
    repo = os.environ.get("HEAL_REPO")
    if repo:
        req["run"]["repo"] = repo[:200]
    return req


def build_audit_request(files: List[Dict[str, str]], page: Dict[str, Any], settings: HealSettings) -> Dict[str, Any]:
    """The POST /v1/audit body: the files to rank and the page inventory to rank them against."""
    return {"client": {"framework": "pytest-voyageur", "language": "python", "version": __version__},
            "source": {"roots": list(settings.roots), "files": files}, "page": page_section(page),
            "test_id_attribute": settings.test_id_attribute}


class NoPage(Exception):
    """There is no page to probe: the test's own is gone and there is no app URL to reopen one on."""


# ---- patch safety ------------------------------------------------------------------------------------------------------
class PatchRefused(Exception):
    """A patch the client will not apply. `fatal` means the service's evidence and the disk disagree (stale hash, wrong
    file, wrong span): the heal ends there. A replacement that is merely unsafe only rules this candidate out."""

    def __init__(self, reason: str, fatal: bool = False):
        super().__init__(reason)
        self.reason, self.fatal = reason, fatal


@dataclass
class PlannedFile:
    rel: str
    original: bytes
    new_text: str


def plan_patches(candidate: Dict[str, Any], sent: Dict[str, str], roots: Tuple[str, ...], root: Path) -> List[PlannedFile]:
    """Check every patch of a candidate (the main one and `also_patches`) and return the new content of each file touched,
    or raise PatchRefused. Nothing is written here."""
    patches = [candidate["patch"], *(candidate.get("also_patches") or [])]
    branches = candidate.get("branches") or None
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for i, p in enumerate(patches):
        rel = p.get("file", "")
        if not relative_under(rel, root, roots) or rel.startswith("/") or ".." in rel.split("/"):
            raise PatchRefused(f"{rel or 'a patch'} is not under the source roots", fatal=True)
        if rel not in sent:
            raise PatchRefused(f"{rel} was not one of the files sent to the service", fatal=True)
        why = check_replacement(p["to"], p["from"], branches, exact_branches=(i == 0))
        if why:
            raise PatchRefused(why)
        if i == 0 and not branches and (_normal(p["to"]) is None or _normal(p["to"]) != _normal(candidate.get("code"))):
            raise PatchRefused("the patch is not the locator that was checked in the live page")
        by_file.setdefault(rel, []).append(p)
    planned: List[PlannedFile] = []
    for rel, group in by_file.items():
        if (root / rel).is_symlink() or not within_roots(root, rel, roots):
            raise PatchRefused(f"{rel} is a symlink or leaves its source root", fatal=True)
        got = read_text_file(root, rel)
        if got is None:
            raise PatchRefused(f"{rel} cannot be read", fatal=True)
        text, raw = got
        digest = _sha(raw)
        spans = sorted(((p["span"]["start"], p["span"]["end"], p) for p in group), key=lambda t: t[0])
        for p in group:
            if p["base_sha256"].lower() != digest or sent[rel].lower() != digest:
                raise PatchRefused(f"{rel} changed since the service saw it (base hash differs)", fatal=True)
        end_prev = -1
        for start, end, p in spans:
            if not (0 <= start <= end <= len(text)) or start < end_prev or text[start:end] != p["from"]:
                raise PatchRefused(f"the span in {rel} does not hold the text the service expected", fatal=True)
            end_prev = end
        nodes = _expression_spans(text)
        if nodes is None:
            raise PatchRefused(f"{rel} does not parse as Python (syntax error), so its spans cannot be checked", fatal=True)
        for start, end, p in spans:
            if (start, end) not in nodes:
                raise PatchRefused(f"the span in {rel} is not one expression of the file's syntax tree", fatal=True)
        new = text
        for start, end, p in reversed(spans):
            new = new[:start] + p["to"] + new[end:]
        if HELPER + "(" in new and not _imports_helper(new):
            new = _with_helper_import(new)
        planned.append(PlannedFile(rel, raw, new))
    return planned


_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")


def _expression_spans(text: str) -> Optional[set]:
    """{(start, end)} character spans of every expression node of `text`, or None when it does not parse. ast columns are
    UTF-8 byte offsets within a line; they are turned into offsets in `text` here."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    lines = _LINE.findall(text)
    starts, total = [], 0
    for line in lines:
        starts.append(total)
        total += len(line)

    def offset(lineno: int, col: int) -> int:
        line = lines[lineno - 1] if lineno - 1 < len(lines) else ""
        return starts[lineno - 1] + len(line.encode("utf-8")[:col].decode("utf-8", "ignore"))

    spans = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.expr) and getattr(node, "end_lineno", None) is not None:
            spans.add((offset(node.lineno, node.col_offset), offset(node.end_lineno, node.end_col_offset)))
    return spans


def _imports_helper(source: str) -> bool:
    """True when `source` has a real `from pytest_voyageur.language import by_language` statement (or the framework's
    `utils.heal_locale` one); a comment or a string that says so does not count."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(n, ast.ImportFrom) and n.module in HELPER_MODULES and n.level == 0
               and any(a.name == HELPER and a.asname is None for a in n.names) for n in ast.walk(tree))


def _with_helper_import(source: str) -> str:
    """`source` with the by_language import added after its last top-level import (or after the module docstring)."""
    lines = source.splitlines(keepends=True)
    insert_at = 0
    try:
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                insert_at = node.end_lineno or insert_at
            elif insert_at == 0 and isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
                insert_at = node.end_lineno or 0                  # the module docstring
    except SyntaxError:
        pass
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    lines.insert(insert_at, HELPER_IMPORT + newline)
    return "".join(lines)


def write_files(root: Path, planned: List[PlannedFile]) -> None:
    for pf in planned:
        _replace(root / pf.rel, pf.new_text.encode("utf-8"))


def revert_files(root: Path, planned: List[PlannedFile]) -> None:
    """Put every file's original bytes back, but only over what the client itself wrote: a file that someone changed since
    (during a re-run that takes minutes) is left as it is and named in the error. One file failing does not stop the others;
    the first error is raised after."""
    first: Optional[BaseException] = None
    for pf in planned:
        try:
            current = (root / pf.rel).read_bytes()
            if current == pf.original:
                continue                                       # never written, or already back
            if current != pf.new_text.encode("utf-8"):
                raise OSError(f"{pf.rel} changed since the heal wrote it, so it was not reverted (restore it yourself)")
            _replace(root / pf.rel, pf.original)
        except Exception as e:                                 # keep restoring the rest
            first = first or e
    if first is not None:
        raise first


@contextmanager
def terminate_as_exit() -> Iterator[None]:
    """For the length of the block SIGTERM raises SystemExit, so the revert that follows a write runs when a CI job is told to
    stop. Does nothing off the main thread (a signal handler can only be set there)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)
    previous = signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


def _replace(path: Path, data: bytes) -> None:
    """Write `data` over a regular file with the file's own mode: a temp file next to it (created with that mode, never
    following a symlink) and an atomic rename. A symlink, or anything that is not a regular file, is refused."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"refusing to write {path.name}: it is not a regular file (a symlink is never followed)")
    mode = stat.S_IMODE(st.st_mode)
    tmp = path.with_name(path.name + ".heal-tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)                                    # the umask may have narrowed it at creation
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- the live page ---------------------------------------------------------------------------------------------------
_LOOSE_SKIP = ("box", "frame", "shadow", "ref", "row", "row_attr", "copy_cut")     # what a lone-element probe may say differently


def _box_free(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in item.items() if k not in _LOOSE_SKIP}


def match_ref(inventory: List[Dict[str, Any]], facts: Dict[str, Any]) -> Optional[str]:
    """The inventory ref of the element the probe described in `facts`: an exact match on everything the probe listed, else
    one that ignores box, frame and shadow. None when no entry or several entries fit (the hit cannot be named)."""
    plain = {k: v for k, v in facts.items() if k != "ref"}
    exact = [e["ref"] for e in inventory if {k: v for k, v in e.items() if k != "ref"} == plain]
    if len(exact) == 1:
        return exact[0]
    loose = [e["ref"] for e in inventory if _box_free(e) == _box_free(facts)]
    return loose[0] if len(loose) == 1 else None


def _origin(url: str) -> Tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port or {"http": 80, "https": 443, "ws": 80, "wss": 443}.get(scheme, 0)
    except ValueError:
        port = -1
    return scheme, (parts.hostname or "").lower(), port


class OriginGuard:
    """Playwright route handler: while `probing` is True, a request to an origin other than the app's is aborted, so the
    probe (or anything a page wires to run while it is evaluated) cannot send what it reads to another site. Outside a
    probe every request goes through untouched. `passthrough` is how an allowed request goes on: `continue_` on a page of
    our own, `fallback` on the test's page, so the routes the test registered still run."""

    def __init__(self, base_url: str = "", passthrough: str = "continue_"):
        self.origin = _origin(base_url) if base_url else None
        self.probing = False
        self.passthrough = passthrough

    def foreign(self, url: str) -> bool:
        return urlsplit(url).scheme.lower() in _NETWORK_SCHEMES and _origin(url) != self.origin

    def handle(self, route: Any, request: Any = None) -> None:
        url = (request or route.request).url
        try:
            if self.probing and self.foreign(url):
                route.abort()
            else:
                getattr(route, self.passthrough)()
        except Exception:                                      # the page closed under the route
            pass

    @contextmanager
    def active(self) -> Iterator[None]:
        self.probing = True
        try:
            yield
        finally:
            self.probing = False


class PageGuard(OriginGuard):
    """The guard for a page we did not open: its route is added for the length of one probe and removed after, and the
    origin is the one the page is on, unless the app's base URL is known."""

    def __init__(self, page: Any, base_url: str = ""):
        super().__init__(base_url, passthrough="fallback")
        self.page, self.fixed = page, bool(base_url)

    @contextmanager
    def active(self) -> Iterator[None]:
        if not self.fixed:
            self.origin = _origin(self.page.url)
        self.page.route("**/*", self.handle)
        try:
            with super().active():
                yield
        finally:
            try:
                self.page.unroute("**/*", self.handle)
            except Exception:                                  # the page closed
                pass


# The typed or chosen text of the page (never sent: it only decides what is cut from the aria snapshot). Open shadow roots and
# same-origin frames are walked; the walk is bounded.
TYPED_VALUES_JS = """() => {
  const out = [], SKIP = /^(button|submit|reset|checkbox|radio|image|file|hidden)$/i;
  const walk = (root, depth) => {
    if (depth > 6 || out.length > 400) return;
    for (const el of root.querySelectorAll('*')) {
      if (out.length > 400) return;
      const tag = el.tagName;
      if (tag === 'INPUT' && !SKIP.test(el.type) && el.value) out.push(String(el.value).slice(0, 400));
      else if (tag === 'TEXTAREA' && el.value) out.push(String(el.value).slice(0, 400));
      else if (tag === 'SELECT') for (const o of el.selectedOptions) out.push(String(o.text || '').slice(0, 400));
      else if (el.isContentEditable && el.getAttribute('contenteditable') !== 'false') out.push(String(el.innerText || '').slice(0, 400));
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
      if (tag === 'IFRAME') { try { if (el.contentDocument) walk(el.contentDocument, depth + 1); } catch (_) { /* cross-origin */ } }
    }
  };
  walk(document, 0);
  return out;
}"""


class PageView:
    """What the client asks of a live page: the probe's inventory with the aria snapshot (`snapshot`) and what a locator
    expression hits (`check`). The subclasses decide which page it is."""

    def __init__(self, settings: HealSettings, probe: str):
        self.settings, self.probe = settings, probe
        self.inventory: List[Dict[str, Any]] = []
        self.language = ""
        self.page: Any = None
        self.guard: OriginGuard = OriginGuard(settings.base_url)
        self._response: Any = None

    def snapshot(self) -> Dict[str, Any]:
        """Run the probe on the page and read its aria snapshot: path, language signals, numbered inventory, viewport."""
        with self.guard.active():
            data = self.page.evaluate(self.probe)
        meta = data.get("page") or {}
        header = None
        try:
            header = self._response.headers.get("content-language") if self._response else None
        except Exception:
            pass
        self.inventory = clean_inventory([{**item, "ref": f"e{i}"} for i, item in enumerate(data.get("items") or [])
                                          if isinstance(item, dict)])
        self.language = page_language(meta.get("lang"), meta.get("browser_lang"), header)
        try:
            with self.guard.active():
                aria = redact_urls(redact_typed_values(self.page.locator("body").aria_snapshot()))
            aria = scrub_values(aria, self.typed_values())
        except Exception:
            aria = None
        view = meta.get("viewport") or {}
        return {"path": urlsplit(self.page.url).path or "/", "inventory": self.inventory, "more": int(data.get("more") or 0),
                "copy_more": int(data.get("copy_more") or 0), "copy_withheld": int(data.get("copy_withheld") or 0),
                "copy_withheld_data": int(data.get("copy_withheld_data") or 0),
                "aria": aria, "viewport": {"w": view.get("width"), "h": view.get("height"), "dpr": meta.get("dpr")},
                "language": {"html_lang": meta.get("lang") or None, "browser_locale": meta.get("browser_lang") or None,
                             "content_language": header}}

    def typed_values(self) -> List[str]:
        """What the page itself holds as typed or chosen text (inputs, textareas, selects, editable regions), read here and
        used only to scrub the aria snapshot; it is never sent. An empty list when the page cannot say (then the aria
        snapshot relies on the role rules alone, and a failure to read here drops the aria snapshot instead)."""
        values = self.page.evaluate(TYPED_VALUES_JS)
        return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []

    def check(self, expression: str) -> Tuple[Optional[int], Optional[str]]:
        """(match count, inventory ref of the one element hit). The ref is None unless the count is 1 and the element can
        be named; the count is None when the expression cannot be run here."""
        loc = build_locator(self.page, expression)
        if loc is None:
            return None, None
        count = loc.count()
        if count != 1:
            return count, None
        with self.guard.active():
            data = loc.evaluate(self.probe)
        facts = (clean_inventory(data.get("items") or []) or [None])[0]
        return 1, (match_ref(self.inventory, facts) if facts else None)


class LivePage(PageView):
    """A fresh Chromium on a route of the app (HEAL_STORAGE_STATE when set), for a failure whose own page is gone. It stays
    open for the whole heal so each candidate can be checked on the same page the inventory came from. Never screenshots."""

    def __init__(self, settings: HealSettings, route: str, probe: str):
        super().__init__(settings, probe)
        self.route = route or "/"
        self._p = self._browser = None

    def __enter__(self) -> "LivePage":
        from playwright.sync_api import sync_playwright
        self._p = sync_playwright().start()
        try:
            self._browser = self._p.chromium.launch()
            ctx = self._browser.new_context(
                base_url=self.settings.base_url,
                **({"storage_state": self.settings.storage_state} if self.settings.storage_state else {}))
            ctx.route("**/*", self.guard.handle)
            self.page = ctx.new_page()
            self._response = self.page.goto(self.route)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        for closer in (getattr(self._browser, "close", None), getattr(self._p, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:
                pass


class AttachedPage(PageView):
    """The page the failing test was using, exactly as the failure left it (a menu open, a tab chosen). Nothing is opened
    or closed: the probe and the candidate checks run on it, and its own routes keep working."""

    def __init__(self, settings: HealSettings, page: Any, probe: str):
        super().__init__(settings, probe)
        self.page = page
        self.guard = PageGuard(page, settings.base_url)

    def __enter__(self) -> "AttachedPage":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


# ---- the heal loop ---------------------------------------------------------------------------------------------------
def _expression_for(candidate: Dict[str, Any], language: str) -> Optional[str]:
    """The locator expression to check live: the candidate's code, or for branch code the branch of the page's language."""
    branches = candidate.get("branches")
    if not branches:
        return candidate["code"]
    return branches.get(language) if language in branches else None


class Healer:
    """Heals one failing test at a time through the service. All collaborators are injected, so a test can drive the whole
    loop with a fake service and a fake page: `verify(test) -> bool` re-runs the exact test, `live_factory(route, probe)`
    opens the page."""

    def __init__(self, service: ServiceClient, settings: HealSettings, verify: Callable[[Dict[str, Any]], bool],
                 root: Optional[Path] = None, live_factory: Optional[Callable[[str, str], Any]] = None,
                 run_id: Optional[str] = None):
        self.service, self.settings, self.verify = service, settings, verify
        self.root = root or Path.cwd()
        self.live_factory = live_factory or (lambda route, probe: LivePage(settings, route, probe))
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.trace: List[Dict[str, Any]] = []                  # every try of the heal in progress, in order
        self.clock: Callable[[], float] = time.monotonic
        self.budget = float(os.environ.get("HEAL_TIME_BUDGET", TIME_BUDGET))

    # one failure -> one decision
    def heal(self, failure: Dict[str, Any]) -> Dict[str, Any]:
        base = {"test": failure["name"]}
        self.trace = []
        try:
            decision = {**base, **self._heal(failure)}
        except HealServiceError as e:
            decision = {**base, "outcome": "needs_human", "reason": "service_error", "why": str(e)}
        except NoPage as e:
            decision = {**base, "outcome": "needs_human", "reason": "no_page", "why": str(e)}
        except Exception as e:                                    # the browser, the disk: this heal ends, the run goes on
            decision = {**base, "outcome": "needs_human", "reason": "client_error",
                        "why": f"the heal client failed ({type(e).__name__}): {str(e)[:200]}"}
        decision["tries"] = self.trace
        return decision

    def _heal(self, failure: Dict[str, Any]) -> Dict[str, Any]:
        roots = self.settings.roots
        stack = stack_from_frames(failure["frames"], roots, self.root) if failure.get("frames") \
            else stack_from_error(failure.get("error", ""), roots, self.root)
        extra = [r for r in (relative_under(str(p), self.root, roots) for p in failure.get("files") or []) if r]
        files = collect_source(stack, roots, self.root, tuple(extra))
        sent = {f["path"]: f["sha256"] for f in files}
        last_pass = self._last_pass(stack)
        route = last_pass.pop("_page") if last_pass else None
        if route is None:
            route = route_from_source(files[0]["content"]) if files else "/"
        probe = self.service.probe_js()
        kept: List[PlannedFile] = []
        tried: Optional[Dict[str, Any]] = None
        started, tries = self.clock(), 0
        with self.live_factory(route, probe) as live:
            snap = live.snapshot()
            request = build_request(failure, files, stack, snap, self.settings, self.root, last_pass, self.run_id)
            resp = self.service.heal(request)
            while resp.get("status") == "try" and resp.get("candidate"):
                if tries >= MAX_TRIES or self.clock() - started > self.budget:
                    reason, why = (("too_many_rounds", f"the service offered more than {MAX_TRIES} candidates")
                                   if tries >= MAX_TRIES else ("time_budget", f"the heal ran past {self.budget:g} s"))
                    return self._decision({**resp, "status": "needs_human", "reason": reason, "why": why}, [], None)
                tries += 1
                cand = resp["candidate"]
                resp, kept = self._try(failure, resp, live, sent, live.language)
                if kept:
                    tried = cand
                    break
        return self._decision(resp, kept, tried)

    def _last_pass(self, stack: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The last green run's fingerprint of the failing line (with the page it was on), or None."""
        if not stack:
            return None
        top = stack[0]
        text = line_of(self.root, top["file"], top["line"])
        store = load_store(heal_dir(self.root) / "fingerprints.json")
        key = call_key(top["file"], text)
        lp = last_pass_for(store, key) if text else None
        if lp is None:
            return None
        lp["_page"] = store["entries"][key].get("page") or "/"
        return lp

    def _note(self, cand: Dict[str, Any], result: str, **extra: Any) -> None:
        """Record one try: the locator, the file, and how it ended (result: unsafe | not_unique | wrong_element | refused |
        reverted | unconfirmed | kept) with what the live page and the re-run said."""
        patch = cand.get("patch") or {}
        self.trace.append({"code": cand.get("code"), "file": patch.get("file"), "strategy": cand.get("strategy"),
                           "result": result, **extra})

    def _try(self, failure: Dict[str, Any], resp: Dict[str, Any], live: Any, sent: Dict[str, str],
             language: str) -> Tuple[Dict[str, Any], List[PlannedFile]]:
        """One candidate: check it live, apply it, re-run the exact test, report. Returns the service's next answer and the
        files kept (non-empty only when the service confirmed `healed`)."""
        cand, heal_id = resp["candidate"], resp["heal_id"]
        expected = (cand.get("expect") or {}).get("ref")
        expression = _expression_for(cand, language)
        if expression is None:
            self._note(cand, "unsafe", why="no branch for the page's language")
            return self._report(heal_id, {"count": None, "error": "no branch for the page's language"}), []
        if not cand.get("branches") and check_replacement(expression, cand["patch"]["from"]):
            self._note(cand, "unsafe", why="the locator is not a safe expression")
            return self._report(heal_id, {"count": None, "error": "the locator is not a safe expression"}), []
        if not cand.get("branches") and _normal(cand["patch"]["to"]) != _normal(expression):
            self._note(cand, "unsafe", why="the patch is not the locator that was checked")
            return self._report(heal_id, {"count": None, "error": "the patch is not the locator that was checked"}), []
        count, ref = live.check(expression)
        if count != 1 or ref is None or ref != expected:       # nothing has been edited yet; no expectation never matches
            report: Dict[str, Any] = {"count": count}
            if ref:
                report["hit"] = {"ref": ref}
            self._note(cand, "not_unique" if count != 1 else "wrong_element", count=count)
            return self._report(heal_id, report), []
        try:
            planned = plan_patches(cand, sent, self.settings.roots, self.root)
            self._check_also_patches(cand, live, language, expected)
        except PatchRefused as e:
            report = {"count": 1, "hit": {"ref": ref}, "test_outcome": "error",
                      "error": f"patch refused by the client: {e.reason}"[:500]}
            self._note(cand, "refused", count=1, why=e.reason)
            if not e.fatal:
                return self._report(heal_id, report), []
            try:
                self.service.result(heal_id, report)
            except HealServiceError:
                pass
            return {**resp, "status": "needs_human", "reason": "patch_refused",
                    "why": f"the client refused the patch: {e.reason}"}, []
        try:
            with terminate_as_exit():                          # SIGTERM during the re-run reverts the edit too
                write_files(self.root, planned)                # inside the try: a partial multi-file write is reverted
                outcome = self.verify(failure)
                passed, detail = bool(outcome), getattr(outcome, "detail", "")
                if not passed:
                    revert_files(self.root, planned)
                nxt = self._report(heal_id, {"count": 1, "hit": {"ref": ref},
                                             "test_outcome": "passed" if passed else "failed"})
        except BaseException:
            revert_files(self.root, planned)                   # a write or verify crash or a lost service: never keep the edit
            raise
        if passed and nxt.get("status") == "healed":
            self._note(cand, "kept", count=1, test_outcome="passed")
            return nxt, planned
        if passed:                                             # the service did not confirm it: do not keep it
            revert_files(self.root, planned)
        self._note(cand, "unconfirmed" if passed else "reverted", count=1, test_outcome="passed" if passed else "failed",
                   **({"rerun": detail} if detail else {}))
        return nxt, []

    @staticmethod
    def _check_also_patches(cand: Dict[str, Any], live: Any, language: str, expected: Optional[str]) -> None:
        """Every other expression the candidate writes is checked in the live page like the main one: it must match exactly
        one element and be the expected one. Anything else refuses the whole candidate (nothing is written)."""
        branches = cand.get("branches")
        for p in cand.get("also_patches") or []:
            if branches:
                parsed = _branch_dict(p["to"])
                expression = parsed[1].get(language) if parsed else None
            else:
                expression = p["to"]
            if expression is None:
                raise PatchRefused("an extra patch has no branch for the page's language")
            count, ref = live.check(expression)
            if count != 1 or ref is None or ref != expected:
                raise PatchRefused(f"an extra patch in {p.get('file')} does not match the one expected element "
                                   f"({count} match(es))")

    def _report(self, heal_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
        return self.service.result(heal_id, report)

    @staticmethod
    def _decision(resp: Dict[str, Any], kept: List[PlannedFile], tried: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """What the heal came to. A heal is reported from what the client did (`tried`, the candidate it wrote and kept, and
        `kept`, the files), never from what the last reply says it was; the reply only decides whether the service confirmed."""
        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
        out: Dict[str, Any] = {"heal_id": resp.get("heal_id"), "reason": resp.get("reason"), "why": resp.get("why"),
                               "rounds": resp.get("round"), "settled_at": resp.get("settled_at"),
                               "usage": {k: _count(usage.get(k)) for k in ("calls", "input_tokens", "output_tokens", "images")}}
        status = resp.get("status")
        if status == "healed" and kept and tried is not None:
            p = tried["patch"]
            out.update(outcome="healed", file=p["file"], to=p["to"], strategy=tried.get("strategy"),
                       tier_movement=tried.get("tier_movement"), why=tried.get("why"), files=[pf.rel for pf in kept])
            out["from"] = p["from"]
            out["source"] = f"heal service (settled at step {resp.get('settled_at')})"
        elif status == "not_drift":
            out["outcome"] = "not_drift"
        else:
            out["outcome"] = "needs_human"
            if status in ("try", "healed"):
                out["reason"] = out["reason"] or "unconfirmed"
                out["why"] = out["why"] or "the service did not confirm the heal"
        return out


def _count(value: Any) -> int:
    """`value` as a count the summary can add: a plain non-negative int below a trillion, else 0 (it came from the service)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 12 else 0


def run_total(decisions: List[Dict[str, Any]]) -> Dict[str, int]:
    """Calls and tokens summed over every decision that carries usage."""
    total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "images": 0}
    for d in decisions:
        for k in total:
            total[k] += _count((d.get("usage") if isinstance(d.get("usage"), dict) else {}).get(k))
    return total


__all__ = ["AttachedPage", "NoPage", "build_audit_request", "page_section", "HealSettings", "HealServiceError", "ServiceClient", "Healer", "LivePage", "PatchRefused",
           "build_locator", "build_request", "check_replacement", "collect_source", "match_ref", "plan_patches", "run_total",
           "stack_from_error", "stack_from_frames", "junit_test_id", "validate_replacement"]
