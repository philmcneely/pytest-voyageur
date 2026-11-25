"""
===============================================================================
End-to-end Fixtures (a tiny site, a service with a scripted model, a project to heal)
===============================================================================
A pytest child process runs a tiny Playwright suite against a tiny local site, with the Voyageur service in this process
answering over 127.0.0.1 from a scripted model. Everything is local: no model endpoint, no network, no GPU. The tests skip
cleanly when Chromium or the service is not installed.

Features:
    ✓ site: a static site on a free 127.0.0.1 port whose pages a test can change between runs (that is the drift)
    ✓ service: starts the in-process service with a model script and tears it down
    ✓ project: the files of a small suite (a page object, tests, a conftest with short timeouts) and `run()` for the child
    ✓ The child gets the real Playwright browsers path (pytester moves HOME)

Environment Variables:
    None.

Author: PMAC
===============================================================================
"""
import http.server
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from tests.support import ScriptedModel, ServiceThread, build_app


def _browsers_root() -> Optional[str]:
    """Where Playwright keeps its browsers, found before pytester changes HOME."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
        return next((str(parent) for parent in exe.parents if parent.name == "ms-playwright"), None)
    except Exception:
        return None


BROWSERS = _browsers_root()


def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


HAS_CHROMIUM = chromium_available()
needs_chromium = pytest.mark.skipif(not HAS_CHROMIUM, reason="no Playwright Chromium build is installed")

PAGE_BASE = ('<html lang="en"><body><form><input id="username" data-testid="login-user" aria-label="Username">'
             '<input id="username-confirm" data-testid="login-user-confirm" aria-label="Confirm username">'
             '<input type="password" id="pw" data-testid="login-pw" aria-label="Password"><button type="submit" id="go">Save</button></form></body></html>')
PAGE_DRIFTED = PAGE_BASE.replace('id="username"', 'id="user-name"')

ACCOUNT_PAGE = '''\
class AccountPage:
    def __init__(self, page, base_url):
        self.page = page
        self.url = f"{base_url}/account.html"

    def open(self):
        self.page.goto(self.url)

    def fill_name(self, name):
        self.page.locator('#username').fill(name)
'''
TEST_ACCOUNT = '''\
from playwright.sync_api import expect

from pages.account_page import AccountPage


def test_fill_name(page, base_url):
    account = AccountPage(page, base_url)
    account.open()
    account.fill_name("Ada")
    expect(page.get_by_test_id("login-user")).to_have_value("Ada")
'''
CONFTEST = '''\
import pytest


@pytest.fixture(autouse=True)
def _short_timeouts(page):
    page.set_default_timeout(1500)
'''


class Site:
    """Static pages on 127.0.0.1; `set` changes a page for the next run."""

    def __init__(self) -> None:
        pages: Dict[str, str] = {}
        self.pages = pages

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:                                      # noqa: N802
                body = pages.get(self.path.split("?")[0])
                if body is None:
                    self.send_error(404)
                    return
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a: Any) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def set(self, name: str, html: str) -> None:
        self.pages["/" + name] = html


@pytest.fixture
def site():
    s = Site()
    s.set("account.html", PAGE_BASE)
    yield s
    s.httpd.shutdown()


@pytest.fixture
def service():
    """start(model) -> (service thread, model): the real service on 127.0.0.1 answering from `model`."""
    started: List[ServiceThread] = []

    def start(model: Any = None, **settings: Any) -> Any:
        model = model if model is not None else ScriptedModel([])
        thread = ServiceThread(build_app(model, **settings)).__enter__()
        started.append(thread)
        return thread, model
    yield start
    for t in started:
        t.__exit__()


class Project:
    """A suite in the pytester directory, and the way to run pytest on it as a child process."""

    def __init__(self, pytester: Any, site: Site, monkeypatch: Any) -> None:
        self.pytester, self.site, self.monkeypatch = pytester, site, monkeypatch
        if BROWSERS:
            monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)
        for name in ("HEAL_SERVICE_URL", "HEAL_SERVICE_TOKEN", "VOYAGEUR_HEAL", "HEAL_RUN", "HEAL_FINGERPRINTS", "BASE_URL",
                     "HEAL_SOURCE_ROOTS", "HEAL_DIR", "HEAL_ARTIFACTS_DIR"):
            monkeypatch.delenv(name, raising=False)
        pytester.makefile(".ini", pytest="[pytest]\npythonpath = .\n")
        pytester.makeconftest(CONFTEST)
        pytester.mkdir("pages")
        pytester.mkdir("tests")
        self.write("pages/account_page.py", ACCOUNT_PAGE)
        self.write("tests/test_account.py", TEST_ACCOUNT)

    @property
    def root(self) -> Path:
        return Path(self.pytester.path)

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def run(self, *args: str, service: Optional[ServiceThread] = None, env: Optional[Dict[str, str]] = None) -> Any:
        if service is not None:
            self.monkeypatch.setenv("HEAL_SERVICE_URL", service.url)
        for k, v in (env or {}).items():
            self.monkeypatch.setenv(k, v)
        return self.pytester.runpytest_subprocess("--base-url", self.site.url, *args)


@pytest.fixture
def project(pytester, site, monkeypatch):
    return Project(pytester, site, monkeypatch)

