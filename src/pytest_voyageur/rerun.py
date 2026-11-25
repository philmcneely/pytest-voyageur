"""
===============================================================================
Exact Test Re-run (the check that decides whether an edit is kept)
===============================================================================
After the client writes a candidate patch it runs the one failing test again, in a fresh pytest process, so the edited files
are imported anew. The edit is kept only when that test ran and passed: an empty selection, a collection error, a skipped
test or a missing report is NOT a pass.

Features:
    ✓ One node id, one process: `python -m pytest <nodeid>` from the rootdir with HEAL_RUN=1, so the plugin (healing,
      fingerprints) is inert in the child and cannot heal inside a heal
    ✓ The options that shape a Playwright run are forwarded (--base-url, --browser, --browser-channel, --device, --slowmo,
      --headed); everything else comes from the project's own ini file, which the child reads like any run
    ✓ Pass = exit code 0 AND a JUnit report with at least one test case, all of them passed
    ✓ A re-run that hangs is killed after HEAL_RERUN_TIMEOUT seconds and counts as failed
    ✓ The child never receives the service token

Environment Variables:
    HEAL_RERUN_TIMEOUT: seconds one re-run may take before it is killed (default 300)
    HEAL_RUN: set to 1 for the child (never set it yourself)

Author: PMAC
===============================================================================
"""
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT = 300.0
_SECRET_ENV = ("HEAL_SERVICE_TOKEN",)


@dataclass
class RerunResult:
    """Whether the exact test passed, and a short line saying how it ended. Truthy when it passed."""
    passed: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.passed


def forwarded_options(config: Any) -> List[str]:
    """The command line options of this run that decide how a Playwright test runs, as arguments for the child."""
    args: List[str] = []

    def get(name: str) -> Any:
        try:
            return config.getoption(name)
        except (ValueError, AttributeError):
            return None

    for dest, flag in (("base_url", "--base-url"), ("browser_channel", "--browser-channel"), ("device", "--device"),
                       ("slowmo", "--slowmo")):
        value = get(dest)
        if value not in (None, "", 0):
            args += [flag, str(value)]
    for browser in get("browser") or []:
        args += ["--browser", str(browser)]
    if get("headed"):
        args.append("--headed")
    return args


def command(config: Any, nodeid: str, report: Path) -> List[str]:
    """The child's command line: this interpreter, pytest, the node id, the forwarded options and the JUnit path."""
    cmd = [sys.executable, "-m", "pytest", nodeid, "-p", "no:cacheprovider", "--tb=short", "-q", f"--junit-xml={report}"]
    has = config.pluginmanager.hasplugin
    if has("xdist"):
        cmd += ["-n", "0"]
    if has("rerunfailures"):
        cmd += ["--reruns", "0"]
    if has("pytest_cov"):
        cmd.append("--no-cov")
    cmd += forwarded_options(config)
    ini = getattr(config, "inipath", None)
    if ini:
        cmd += ["-c", str(ini)]
    cmd += ["--rootdir", str(config.rootpath)]
    return cmd


def read_report(path: Path) -> Optional[List[Dict[str, str]]]:
    """[{name, classname, outcome, message}] of every test case in a JUnit file, or None when it cannot be read."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    cases = []
    for tc in root.iter("testcase"):
        bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
        if bad is not None:
            outcome, message = "failed", (bad.get("message") or "")
        elif tc.find("skipped") is not None:
            outcome, message = "skipped", ""
        else:
            outcome, message = "passed", ""
        cases.append({"name": tc.get("name") or "", "classname": tc.get("classname") or "", "outcome": outcome,
                      "message": message})
    return cases


def judge(returncode: int, cases: Optional[List[Dict[str, str]]]) -> RerunResult:
    """Pass only when pytest exited 0 and the report holds at least one test case and every case passed."""
    if cases is None:
        return RerunResult(False, "the re-run left no readable report")
    if not cases:
        return RerunResult(False, "the re-run selected no test")
    bad = [c for c in cases if c["outcome"] != "passed"]
    if bad:
        first = bad[0]
        return RerunResult(False, (f"{first['outcome']}: " + " ".join(first["message"].split()))[:200])
    if returncode != 0:
        return RerunResult(False, f"pytest exited with {returncode}")
    return RerunResult(True, "passed")


def run_exact(config: Any, nodeid: str, root: Optional[Path] = None, timeout: Optional[float] = None,
              env: Optional[Dict[str, str]] = None) -> RerunResult:
    """Run `nodeid` alone in a fresh process and say whether it passed."""
    root = Path(root or config.rootpath)
    limit = timeout if timeout is not None else float(os.environ.get("HEAL_RERUN_TIMEOUT", DEFAULT_TIMEOUT))
    child_env = {k: v for k, v in (env if env is not None else os.environ).items() if k not in _SECRET_ENV}
    child_env["HEAL_RUN"] = "1"
    with tempfile.TemporaryDirectory(prefix="voyageur-") as tmp:
        report = Path(tmp) / "report.xml"
        try:
            proc = subprocess.run(command(config, nodeid, report), cwd=str(root), env=child_env, capture_output=True,
                                  timeout=limit)
        except subprocess.TimeoutExpired:
            return RerunResult(False, f"the re-run was killed after {limit:g}s")
        except OSError as e:
            return RerunResult(False, f"the re-run could not start ({type(e).__name__})")
        return judge(proc.returncode, read_report(report))
