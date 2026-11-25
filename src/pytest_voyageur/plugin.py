"""
===============================================================================
pytest-voyageur Plugin (heal a drifted Playwright locator when its test fails)
===============================================================================
Registered through the `pytest11` entry point, so `pip install pytest-voyageur` is all it takes. Without `--heal` (or
VOYAGEUR_HEAL=1) the plugin is inert: no hook does anything, no Playwright class is touched and the service is never called.

With it, a failing test's page is probed while the failure is still on screen, the Voyageur service picks the element the broken
step meant and writes a better locator, and the client (client.py) checks it live, applies the patch under its safety rules,
re-runs the exact test in a fresh process and keeps the edit only when the service confirms the heal. Verified edits stay in the
working tree for review (`git diff`); the failing test itself is still reported as failed in the run that needed the heal.

Features:
    ✓ `--heal` / VOYAGEUR_HEAL=1: heal on failure; `--heal-url`, `--heal-max N` (default 10 failures a run)
    ✓ The test's own `page` fixture is probed (a menu open, a tab chosen stays as the failure left it); a test with no usable
      page gets a fresh Chromium on BASE_URL / --base-url instead
    ✓ A failure healed earlier in the run by a shared page object is confirmed by re-running it, not healed twice
    ✓ One heal at a time per repository (a file lock), so parallel pytest-xdist workers never edit a file at once
    ✓ `voyageur` fixture: `heal(page, error)` and `audit(page, files)` for direct use, with or without --heal
    ✓ `@pytest.mark.no_heal`: never heal this test
    ✓ Terminal summary: every heal (file, before, after, step, tokens) and every needs_human with its reason, each try of
      every heal, and heal-summary.json under .heal/ (HEAL_ARTIFACTS_DIR to move it)
    ✓ `--heal-record` / HEAL_FINGERPRINTS=on: record what each locator hit on a green run (see fingerprints.py)
    ✓ A service that cannot be reached turns healing off with one line in the header; it never fails the run

Environment Variables:
    VOYAGEUR_HEAL: "1" to heal on failure (same as --heal)
    HEAL_SERVICE_URL: the Voyageur service (default http://127.0.0.1:8765); HEAL_SERVICE_TOKEN: its bearer token
    HEAL_ARTIFACTS_DIR: where heal-summary.json goes (default: the .heal directory)
    HEAL_RERUN_TIMEOUT: seconds a verification re-run may take (default 300)
    HEAL_RUN: set by the plugin for the re-run; healing and recording are off while it is set
    The rest (HEAL_SOURCE_ROOTS, HEAL_TEST_ID_ATTRIBUTE, HEAL_STORAGE_STATE, BASE_URL, HEAL_FINGERPRINTS, HEAL_DIR, ...) are
    documented in client.py and fingerprints.py.

Author: PMAC
===============================================================================
"""
import inspect
import json
import os
import time
import traceback
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from . import fingerprints
from .client import (AttachedPage, HealServiceError, HealSettings, Healer, LivePage, NoPage, ServiceClient, build_audit_request,
                     read_text_file, run_total, within_roots)
from .fingerprints import heal_dir, relative_under, source_roots
from .rerun import run_exact

try:                                     # POSIX file locking; without it heals in parallel workers are not serialized
    import fcntl
except ImportError:                      # pragma: no cover
    fcntl = None

DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_MAX = 10
PROPERTY = "voyageur"
STATE: "pytest.StashKey[HealState]" = pytest.StashKey()
_RECORDER: List[Optional[fingerprints.Recorder]] = [None]       # the one recorder of this process (a session has one)
_YES = ("1", "true", "on", "yes")
_BAD_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}               # control, format (bidi, zero width), line and paragraph separators


# ---- options -------------------------------------------------------------------------------------------------------------
def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup("voyageur", "Voyageur locator healing")
    group.addoption("--heal", action="store_true", default=False,
                    help="heal a drifted locator when its test fails (needs a running Voyageur service)")
    group.addoption("--heal-url", default=None, metavar="URL",
                    help=f"the Voyageur service (default HEAL_SERVICE_URL, else {DEFAULT_URL})")
    group.addoption("--heal-max", type=int, default=DEFAULT_MAX, metavar="N",
                    help=f"heal at most N failing tests in one run (default {DEFAULT_MAX}); the rest are listed as needs_human")
    group.addoption("--heal-record", action="store_true", default=False,
                    help="record what each locator hits on green tests (same as HEAL_FINGERPRINTS=on)")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _YES


def heal_requested(config: Any) -> bool:
    """True when healing is asked for and this is not the re-run of a heal."""
    return not os.environ.get("HEAL_RUN") and (bool(config.getoption("heal", False)) or _flag("VOYAGEUR_HEAL"))


def record_requested(config: Any) -> bool:
    return not os.environ.get("HEAL_RUN") and (bool(config.getoption("heal_record", False))
                                              or os.environ.get("HEAL_FINGERPRINTS", "").strip().lower() == "on")


# ---- the rendering of a decision -----------------------------------------------------------------------------------------
def _tokens(d: Dict[str, Any]) -> str:
    u = d.get("usage") or {}
    return f"{u.get('calls', 0)} model call(s), {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out tokens"


def _one_line(value: Any, limit: int = 300) -> str:
    """`value` on one line with control, bidi and other format characters turned into spaces (it came from the service)."""
    text = "".join(" " if unicodedata.category(c) in _BAD_CATEGORIES else c for c in str(value if value is not None else ""))
    return " ".join(text.split())[:limit]


def describe_tries(decision: Dict[str, Any]) -> List[str]:
    lines = []
    for i, t in enumerate(decision.get("tries") or [], 1):
        extra = " ".join(f"{k}={_one_line(t[k], 120)}" for k in ("count", "test_outcome", "why", "rerun") if k in t)
        lines.append(f"  try {i}: {_one_line(t.get('code'), 160)} in {_one_line(t.get('file'), 200)}: {_one_line(t.get('result'), 40)}"
                     + (f" ({extra})" if extra else ""))
    return lines


def describe(decision: Dict[str, Any]) -> List[str]:
    """The lines that say what happened to one failing test (terminal summary and the test's own report section)."""
    test, outcome = _one_line(decision.get("test"), 200), _one_line(decision.get("outcome"), 40)
    if outcome == "healed":
        head = [f"[healed] {test}: {_one_line(decision.get('file'), 200)}: {_one_line(decision.get('from'), 200)} -> "
                f"{_one_line(decision.get('to'), 200)}  ({_one_line(decision.get('source') or _tokens(decision), 200)})"]
    else:
        reason = f" [{_one_line(decision['reason'], 60)}]" if decision.get("reason") else ""
        head = [f"[{outcome}]{reason} {test}: {_one_line(decision.get('why'))}"]
        if decision.get("hint"):
            head.append(f"  hint: {_one_line(decision['hint'])}")
    return head + describe_tries(decision)


# ---- the session ---------------------------------------------------------------------------------------------------------
@contextmanager
def heal_lock(root: Path) -> Iterator[None]:
    """One heal at a time in this repository (pytest-xdist workers share the files they edit)."""
    if fcntl is None:
        yield
        return
    directory = heal_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    fingerprints.ensure_ignore(directory)
    with open(directory / ".heal-run.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def usable_page(page: Any) -> bool:
    """True for a live synchronous Playwright page (the async API cannot be driven from a pytest hook)."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or inspect.iscoroutinefunction(evaluate) or not hasattr(page, "route"):
        return False
    try:
        return not page.is_closed()
    except Exception:
        return False


def error_text(excinfo: Any) -> str:
    try:
        return str(excinfo.getrepr(style="short", chain=False, funcargs=False))
    except Exception:
        return str(getattr(excinfo, "exconly", lambda: "")())


def frames_of(excinfo: Any) -> List[Tuple[str, int]]:
    """(path, line) of every frame of a traceback, outermost first. Accepts a pytest ExceptionInfo or an exception."""
    entries = getattr(excinfo, "traceback", None)
    if entries is not None:
        return [(str(e.path), e.lineno + 1) for e in entries]
    tb = getattr(excinfo, "__traceback__", None)
    return [(f.filename, f.lineno) for f in traceback.extract_tb(tb)] if tb else []


class HealState:
    """Everything one pytest session knows about healing: the settings, the service, every decision made."""

    def __init__(self, config: Any):
        self.config = config
        self.root = Path(config.rootpath)
        self.enabled = heal_requested(config)
        self.recording = record_requested(config)
        self.run_id = uuid.uuid4().hex[:12]
        self.decisions: List[Dict[str, Any]] = []
        self.healed_files: set = set()
        self.attempts = 0
        self.test_dirs: Tuple[str, ...] = ()
        self.recorder: Optional[fingerprints.Recorder] = None
        self._settings: Optional[HealSettings] = None
        self._service: Optional[ServiceClient] = None
        self._service_error: Optional[str] = None
        self._checked = False

    # settings and service
    @property
    def settings(self) -> HealSettings:
        if self._settings is None:
            s = HealSettings.from_env(os.environ, self.test_dirs)
            s.url = (self.config.getoption("heal_url", None) or s.url or DEFAULT_URL).rstrip("/")
            if not s.base_url:
                s.base_url = self._base_url_option()
            self._settings = s
        return self._settings

    @property
    def service(self) -> ServiceClient:
        if self._service is None:
            self._service = ServiceClient(self.settings.url, self.settings.token)
        return self._service

    def _base_url_option(self) -> str:
        """pytest-base-url's `--base-url` (or its ini value), when that plugin is installed."""
        value = self.config.getoption("base_url", None)
        if not value:
            try:
                value = self.config.getini("base_url")
            except (ValueError, KeyError):
                value = ""
        return str(value or "").strip()

    def service_ready(self) -> bool:
        """True when the service answers. Asked once; a service that is down turns healing off for the run."""
        if not self._checked:
            self._checked = True
            try:
                self.service.capabilities()
            except HealServiceError as e:
                self._service_error = str(e)
        return self._service_error is None

    @property
    def service_error(self) -> Optional[str]:
        return self._service_error

    def collect_dirs(self, items: List[Any]) -> None:
        """The top directory of every collected test file: those are source roots too."""
        dirs = []
        for item in items:
            try:
                parts = Path(str(item.path)).resolve().relative_to(self.root.resolve()).parts
            except (ValueError, OSError, AttributeError):
                continue
            if len(parts) > 1 and parts[0] not in dirs:
                dirs.append(parts[0])
        self.test_dirs = tuple(dirs)
        self._settings = None
        if self.recorder is not None:
            self.recorder.roots = source_roots(os.environ, fingerprints.DEFAULT_ROOTS, self.test_dirs)

    # one failing test
    def live_factory(self, page: Any) -> Any:
        settings = self.settings

        def factory(route: str, probe: str) -> Any:
            if usable_page(page):
                return AttachedPage(settings, page, probe)
            if not settings.base_url:
                raise NoPage("the test's page is gone and no app URL is known to reopen it (set --base-url or BASE_URL)")
            return LivePage(settings, route, probe)
        return factory

    def outside_roots(self, frames: List[Tuple[str, int]]) -> List[str]:
        """Project files of the failing stack that the source roots do not cover (so they could not be sent or patched)."""
        roots, seen = self.settings.roots, []
        for path, _ in frames:
            try:
                rel = Path(path).resolve().relative_to(self.root.resolve()).as_posix()
            except (ValueError, OSError):
                continue
            if not set(Path(rel).parts) & fingerprints.NEVER_SOURCE and relative_under(rel, self.root, roots) is None \
                    and rel not in seen:
                seen.append(rel)
        return seen

    def heal(self, nodeid: str, name: str, excinfo: Any, page: Any) -> Dict[str, Any]:
        """One decision for one failing test. Never raises."""
        failure = {"name": name, "nodeid": nodeid, "error": error_text(excinfo), "frames": frames_of(excinfo),
                   "files": [str(self.config.rootpath / nodeid.split("::", 1)[0])]}       # the test's own file, always sent
        base = {"test": nodeid}
        try:
            if self.attempts >= max(0, int(self.config.getoption("heal_max", DEFAULT_MAX))):
                return {**base, "outcome": "needs_human", "reason": "beyond_max",
                        "why": f"not attempted: beyond --heal-max {self.config.getoption('heal_max')}", "tries": []}
            self.attempts += 1
            settings = self.settings
            healer = Healer(self.service, settings, lambda f: run_exact(self.config, f["nodeid"], self.root),
                            root=self.root, live_factory=self.live_factory(page), run_id=self.run_id)
            with heal_lock(self.root):
                decision = self._same_fix(failure) or healer.heal(failure)
            decision["test"] = nodeid
            self.decisions.append(decision)
            if decision.get("outcome") == "healed":
                self.healed_files.update(decision.get("files") or [decision.get("file")])
            elif (outside := self.outside_roots(failure["frames"])):
                decision["hint"] = ("source files outside the source roots were not sent: " + ", ".join(outside[:5])
                                    + " (set HEAL_SOURCE_ROOTS, `.` is the whole repository)")
            return decision
        except Exception as e:                                          # a bug here must never fail or hang a test run
            return {**base, "outcome": "needs_human", "reason": "client_error", "tries": [],
                    "why": f"the heal client failed ({type(e).__name__}): {str(e)[:200]}"}

    def _same_fix(self, failure: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """When an earlier heal of this run edited a file this failure's stack runs through, the edit may already be the fix
        (a page object shared by several tests): re-run the test and, if it passes, say so instead of healing it twice."""
        if not self.healed_files:
            return None
        touched = {relative_under(str(p), self.root, self.settings.roots) for p, _ in failure["frames"]} - {None}
        if not touched & self.healed_files or not run_exact(self.config, failure["nodeid"], self.root):
            return None
        first = next(d for d in self.decisions if d.get("outcome") == "healed")
        return {"test": failure["name"], "outcome": "healed", "file": first["file"], "from": first["from"], "to": first["to"],
                "files": first.get("files", []), "usage": {}, "tries": [],
                "source": f"same fix as {first['test']} (shared locator), re-run passed"}


# ---- hooks ---------------------------------------------------------------------------------------------------------------
def pytest_configure(config: Any) -> None:
    config.addinivalue_line("markers", "no_heal: never heal this test's locators (pytest-voyageur)")
    state = HealState(config)
    config.stash[STATE] = state
    if state.recording:
        try:
            state.recorder = _RECORDER[0] = fingerprints.Recorder(root=state.root)
            fingerprints.install(state.recorder)
        except Exception:
            state.recorder = _RECORDER[0] = None


def pytest_unconfigure(config: Any) -> None:
    state = config.stash.get(STATE, None)
    if state is not None and state.recorder is not None:
        fingerprints.uninstall()
        _RECORDER[0] = None


def pytest_collection_finish(session: Any) -> None:
    state = session.config.stash.get(STATE, None)
    if state is not None and (state.enabled or state.recording):
        state.collect_dirs(list(session.items))


def pytest_report_header(config: Any) -> Optional[List[str]]:
    state = config.stash.get(STATE, None)
    lines = []
    if state is not None and state.enabled:
        if state.service_ready():
            lines.append(f"voyageur: healing on, service {state.settings.url}")
        else:
            lines.append(f"voyageur: --heal is on but {state.service_error}; healing is off for this run")
    if state is not None and state.recording:
        lines.append("voyageur: recording fingerprints of green tests into .heal/")
    return lines or None


def pytest_runtest_setup(item: Any) -> None:
    state = item.config.stash.get(STATE, None)
    if state is not None and state.recorder is not None:
        state.recorder.start_test(item)


def pytest_runtest_logreport(report: Any) -> None:
    # the outcomes of setup, call and teardown decide whether the test was clean green; the recorder writes at teardown
    recorder = _RECORDER[0]
    if recorder is None:
        return
    recorder.outcomes[report.when] = "rerun" if getattr(report, "rerun", False) else report.outcome
    if report.when == "teardown":
        recorder.finish_test()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Iterator[None]:
    outcome = yield
    state = item.config.stash.get(STATE, None)
    if state is None or not state.enabled or call.when != "call" or call.excinfo is None:
        return
    report = outcome.get_result()
    if not report.failed or item.get_closest_marker("no_heal") is not None:
        return
    if not state.service_ready():
        return
    page = getattr(item, "funcargs", {}).get("page")
    decision = state.heal(item.nodeid, item.name, call.excinfo, page)
    report.sections.append(("voyageur", "\n".join(describe(decision))))
    report.user_properties.append((PROPERTY, json.dumps(decision, default=str)))


def decisions_of(terminalreporter: Any) -> List[Dict[str, Any]]:
    """Every decision carried by a report of this run (also the ones a pytest-xdist worker made)."""
    found, seen = [], set()
    for reports in terminalreporter.stats.values():
        for rep in reports:
            for key, value in getattr(rep, "user_properties", None) or []:
                if key == PROPERTY and id(rep) not in seen:
                    seen.add(id(rep))
                    try:
                        found.append(json.loads(value))
                    except ValueError:
                        pass
    return found


def write_summary(root: Path, decisions: List[Dict[str, Any]], started: str, run_id: str) -> Optional[Path]:
    """heal-summary.json: counts, usage total and every decision. None when it cannot be written."""
    directory = Path(os.environ.get("HEAL_ARTIFACTS_DIR") or heal_dir(root))
    counts = {k: sum(1 for d in decisions if d.get("outcome") == k) for k in ("healed", "needs_human", "not_drift")}
    summary = {"run_id": run_id, "started": started, "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "failures": len(decisions), "counts": counts, "usage": run_total(decisions), "decisions": decisions}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if directory == heal_dir(root):
            fingerprints.ensure_ignore(directory)
        path = directory / "heal-summary.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: Any) -> None:
    state = config.stash.get(STATE, None)
    if state is None or not state.enabled or hasattr(config, "workerinput") or not state.service_ready():
        return
    decisions = decisions_of(terminalreporter)
    terminalreporter.section("voyageur: heal summary")
    if not decisions:
        terminalreporter.write_line("no failing test needed healing")
        return
    healed = [d for d in decisions if d.get("outcome") == "healed"]
    other = [d for d in decisions if d.get("outcome") != "healed"]
    if healed:
        terminalreporter.write_line(f"healed {len(healed)} (the edits are in your working tree: review them with git diff)")
        for d in healed:
            for line in describe(d):
                terminalreporter.write_line(line, green=line.startswith("[healed]"))
    if other:
        terminalreporter.write_line(f"needs a person {len(other)}")
        for d in other:
            for line in describe(d):
                terminalreporter.write_line(line, yellow=line.startswith("["))
    total = run_total(decisions)
    terminalreporter.write_line(f"run total: {total['calls']} model call(s), {total['input_tokens']} input / "
                                f"{total['output_tokens']} output tokens")
    path = write_summary(state.root, decisions, time.strftime("%Y-%m-%dT%H:%M:%S%z"), state.run_id)
    if path is not None:
        terminalreporter.write_line(f"summary: {path}")


# ---- the fixture ---------------------------------------------------------------------------------------------------------
class Voyageur:
    """What the `voyageur` fixture hands a test: the service, ready for direct use. Works with or without --heal."""

    def __init__(self, state: HealState, request: Any):
        self._state, self._request = state, request

    @property
    def settings(self) -> HealSettings:
        return self._state.settings

    @property
    def service(self) -> ServiceClient:
        return self._state.service

    def _page(self, page: Any) -> Any:
        page = page if page is not None else self._request.getfixturevalue("page")
        if not usable_page(page):
            raise NoPage("voyageur needs a live synchronous Playwright page")
        return page

    def heal(self, page: Any = None, error: Any = None) -> Dict[str, Any]:
        """Heal the failure `error` (an exception, a pytest ExceptionInfo, or text) on `page`, the same way the plugin heals a
        failing test, and return the decision (outcome: healed | needs_human | not_drift). The exact test is re-run to verify
        it, so call this from a failure hook or a conftest, not from inside the test being healed."""
        if os.environ.get("HEAL_RUN"):
            return {"test": self._request.node.nodeid, "outcome": "needs_human", "reason": "inside_heal_run", "tries": [],
                    "why": "this is the re-run of a heal; healing is off"}
        if error is None:
            raise ValueError("voyageur.heal needs the error to heal")
        node = self._request.node
        if isinstance(error, BaseException):
            error = _Err(error, "".join(traceback.format_exception(error)))
        elif isinstance(error, str):
            error = _Err(None, error)
        return self._state.heal(node.nodeid, node.name, error, self._page(page))

    def audit(self, page: Any = None, files: Optional[List[Any]] = None) -> Dict[str, Any]:
        """Rank every locator call of `files` (default: the requesting test's own file) against `page`'s inventory. No model
        is called and no file is edited. Returns the service's audit (`locators`, `summary`)."""
        view = AttachedPage(self.settings, self._page(page), _vendored())
        snapshot = view.snapshot()
        sent, roots, root = [], self.settings.roots, self._state.root
        for f in files or [self._request.node.path]:
            rel = relative_under(str(f), root, roots)
            got = read_text_file(root, rel) if rel and within_roots(root, rel, roots) else None
            if got is None:
                raise ValueError(f"{f} is not a readable UTF-8 file under the source roots {list(roots)}")
            sent.append({"path": rel, "content": got[0]})
        return self.service.audit(build_audit_request(sent, snapshot, self.settings))


class _Err:
    """The little of an ExceptionInfo that the heal needs, for an error that did not come from pytest."""

    def __init__(self, exc: Optional[BaseException], text: str):
        self._text = text
        self.__traceback__ = exc.__traceback__ if exc is not None else None

    def getrepr(self, **_: Any) -> str:
        return self._text

    def exconly(self) -> str:
        return self._text


def _vendored() -> str:
    from .client import vendored_probe
    return vendored_probe()


@pytest.fixture
def voyageur(request: Any) -> Voyageur:
    """The Voyageur service for direct use: `voyageur.heal(page, error)` and `voyageur.audit(page, files)`."""
    return Voyageur(request.config.stash[STATE], request)
