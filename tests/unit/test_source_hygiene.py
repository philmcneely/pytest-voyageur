"""Unit: nothing in the repository names a host, an address, a key, a local path, a personal address or an internal product
(the repository is meant to be public)."""
import codecs
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# ---- nothing that must not be public ----------------------------------------------------------------------------
# The internal names (the maintainer's public GitHub account is not one: the repository URLs need it), stored rot13 so this file does not itself name them (the repository is meant to be public).
_INTERNAL_ROT13 = "xvex|uny9000|uny|xnenxhev|fragvary|fjvgpuobneq|syrrg|qenpbavf|dn[-_ ]?ehaare|dn[-_ ]?pbagenpgf|rkrphgbe|znp[- ]?fghqvb|jbce|ubzryno"
INTERNAL = re.compile(r"\b(?:" + codecs.decode(_INTERNAL_ROT13, "rot13") + r")\b", re.I)
IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])(/\d{1,2})?")
KEYS = re.compile(r"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{12,}|xox[bp]-[A-Za-z0-9-]{20,}|"
                  r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
                  r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*['\"][A-Za-z0-9+/_-]{20,}['\"]", re.I)
HOSTNAME = re.compile(r"\b[a-z0-9-]+\.(?:local|lan|internal|home|corp|ts\.net)\b", re.I)
# Docker's documented name for the host as seen from a container: the same on every machine, so it names no network of ours.
# Exact match only (the whole dotted name); anything else ending in .internal is still a finding.
# A dot here is any of the Unicode full stops a resolver folds to "." (U+FF0E, U+3002, U+FF61), so none of them can extend the name.
PUBLIC_HOSTNAMES = re.compile(r"(?<![\w.．。｡-])host\.docker\.internal(?![.．。｡]?[\w-])", re.I)
THIS_FILE = Path(__file__).resolve()


def files_to_scan():
    skip_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".heal", "build", "dist", "pytest_voyageur.egg-info"}
    exts = {".py", ".js", ".json", ".md", ".toml", ".txt", ".html", ".css", ".yml", ".yaml", ".cfg", ".sha256", ""}
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip_dirs) and p != THIS_FILE and p.suffix in exts:
            yield p


def hostname_hits(line: str):
    """The internal-looking host names in one line, Docker's own host name left out."""
    return [m.group(0) for m in HOSTNAME.finditer(PUBLIC_HOSTNAMES.sub("", line))]


def lines_of(p: Path):
    return enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), start=1)


def test_the_scan_reaches_the_readme_the_policies_the_source_and_the_tests():
    scanned = {p.relative_to(ROOT).parts[0] for p in files_to_scan()}
    assert {"README.md", "CHANGELOG.md", "SECURITY.md", "src", "tests", "scripts", ".github"} <= scanned


def test_no_file_names_an_internal_host_or_product():
    hits = [(str(p.relative_to(ROOT)), i, m.group(0)) for p in files_to_scan() for i, line in lines_of(p)
            for m in INTERNAL.finditer(line)]
    assert not hits, hits


def test_no_file_holds_a_lan_address_or_an_internal_hostname():
    """A 192.168.* address, or any address outside loopback, the private blocks and the documentation ranges, is a
    finding. A block written as a prefix (10.0.0.0/8) is the standard, not an address."""
    hits = []
    for p in files_to_scan():
        for i, line in lines_of(p):
            for m in IPV4.finditer(line):
                a, b = int(m.group(1)), int(m.group(2))
                if m.group(5) is not None:
                    continue
                if (a == 192 and b == 168) or a not in (127, 0, 10, 172, 198, 203, 100, 192):
                    hits.append((str(p.relative_to(ROOT)), i, m.group(0)))
            hits += [(str(p.relative_to(ROOT)), i, h) for h in hostname_hits(line)]
    assert not hits, hits


def test_only_dockers_own_host_name_is_left_out():
    assert hostname_hits("http://host.docker.internal:11434/v1") == []
    assert hostname_hits("HOST.Docker.INTERNAL") == []
    assert hostname_hits("point it at host.docker.internal.") == []                     # the end of a sentence
    assert hostname_hits("db.internal") == ["db.internal"]
    assert hostname_hits("host.docker.internal db.internal") == ["db.internal"]
    assert hostname_hits("my-host.docker.internal") == ["docker.internal"]
    assert hostname_hits("xhost.docker.internal") == ["docker.internal"]
    assert hostname_hits("foo.host.docker.internal") == ["docker.internal"]
    assert hostname_hits("host.docker.internal.lan") == ["docker.internal"]
    assert hostname_hits("host.docker.internal-x.lan") == ["docker.internal", "-x.lan"]
    assert hostname_hits("host.docker.internal.evil.lan") == ["docker.internal", "evil.lan"]
    for dot in ("．", "。", "｡"):                                        # a Unicode full stop extends the name too
        assert hostname_hits(f"host.docker.internal{dot}lan") == ["docker.internal"], dot


def test_no_file_holds_a_key_or_a_token():
    hits = [(str(p.relative_to(ROOT)), i) for p in files_to_scan() for i, line in lines_of(p) if KEYS.search(line)]
    assert not hits, hits


def test_the_scanners_catch_what_they_are_meant_to():
    def plain(word):                                   # words are rot13 here too, for the same reason
        return codecs.decode(word, "rot13")
    assert INTERNAL.search("ssh " + plain("xvex")) and INTERNAL.search(plain("dn-ehaare")) and INTERNAL.search(
        "the " + plain("Syrrg") + " map")
    assert not INTERNAL.search("hallmark") and not INTERNAL.search("exhale")
    lan = ".".join(("192", "168", "1", "2"))
    assert IPV4.search("http://" + lan + ":80") and HOSTNAME.search("box.local") and KEYS.search("sk-" + "a" * 24)


LOCAL_PATH = re.compile(r"/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+|[A-Za-z]:\\Users\\")
PERSONAL_ADDRESS = re.compile(r"[\w.+-]+@(?:gmail|googlemail|hotmail|outlook|icloud|yahoo)\.com", re.I)


def test_no_file_holds_a_local_path_or_a_personal_address():
    hits = [(str(p.relative_to(ROOT)), i) for p in files_to_scan() for i, line in lines_of(p)
            if LOCAL_PATH.search(line) or PERSONAL_ADDRESS.search(line)]
    assert not hits, hits


def test_the_path_and_address_scanners_catch_what_they_are_meant_to():
    assert LOCAL_PATH.search("/" + "Users/someone/git") and LOCAL_PATH.search("/" + "home/someone")
    assert PERSONAL_ADDRESS.search("someone" + "@gmail.com") and not PERSONAL_ADDRESS.search("a@users.noreply.github.com")
    assert not LOCAL_PATH.search("pages/account_page.py")
