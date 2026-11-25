"""
===============================================================================
Heal Redaction (what a person typed never leaves the browser for the heal service)
===============================================================================
Playwright renders the current value of a control in its accessibility snapshot (`- textbox "Pw": hunter2`, password
fields included), and a page probe is only as clean as the script that ran. Everything the heal client sends about the
live page therefore passes through here first, on the client's side of the wire, whatever the service or its probe
promises. The rule set below is a copy of the service's `redact_typed_values` (same left-to-right pass, no regex), kept in
this package so the privacy line does not depend on the service being honest or current.

Features:
    ✓ redact_typed_values(yaml_text): an accessibility snapshot without what a person typed. The value after the name of
      textbox, searchbox, spinbutton, slider and combobox lines goes (`- textbox "Email": me@example.com`); the role,
      name and states ([checked], [disabled]) stay, whatever the key's YAML quoting (plain, 'single' with '' inside,
      "double" with \" inside). Nested typed children go too: everything under a textbox or searchbox bar `- /property`
      lines, `text` and `/value` under the other value roles, and block-scalar continuations. /placeholder and combobox
      options stay. Linear time: one pass over the lines, every scan bounded by the line
    ✓ clean_inventory(items): the probe's element inventory cut to an allowlist of keys; any key named value or
      defaultValue (any case, any prefix) is dropped, so a probe that learned to read input values cannot carry them out
    ✓ redact_urls(yaml_text): the query and fragment of every `- /url:` line of an aria snapshot cut
    ✓ scrub_values(text, values): the typed text the page itself holds (inputs, textareas, selects, editable regions) removed
      from the aria snapshot wherever it shows, for the roles redact_typed_values does not know are inputs
    ✓ Nothing here talks to the network or the browser: plain functions over plain data

Environment Variables:
    None.

Author: PMAC
===============================================================================
"""
import re
from typing import Any, Dict, List, Optional, Tuple

# The keys a probe item may carry out of the client. `ref` is added by the client itself. data_<name> keys are allowed
# when the name is not a value, a credential or an address (see _DROP_KEY).
INVENTORY_KEYS = frozenset({
    "ref", "tag", "type", "role", "name", "aria_label", "label", "placeholder", "id", "testid", "data_test", "data_qa",
    "data_cy", "class", "name_attr", "for_attr", "title", "alt", "text", "disabled", "hidden", "box", "row", "row_attr",
    "frame", "shadow", "copy_only", "copy_cut", "copy_wrap", "top_at_center", "pw_text", "pw_cut",
    "text_cut", "name_cut",
})
# Keys the probe sets to true or false and nothing else: a string under one of these is not the probe's, so it is dropped.
_BOOL_KEYS = frozenset({"text_cut", "name_cut"})
_DATA_KEY = re.compile(r"^data_[a-z0-9_]{1,60}$")
_DROP_KEY = re.compile(r"value|token|csrf|auth|session|secret|email|password|cookie", re.I)
_BOX_KEYS = ("x", "y", "width", "height")
_FIELD_MAX = 400


# Roles whose text after the colon is the control's current value; other roles' text after the colon is page prose.
_VALUE_ROLES = frozenset({"textbox", "searchbox", "spinbutton", "slider", "combobox", "textarea"})
# Roles that hold free text: everything nested under them (text, links, ...) was typed, bar `/property` lines.
_FREE_TEXT_ROLES = frozenset({"textbox", "searchbox", "textarea"})


def _quoted_end(s: str, i: int) -> int:
    """Index just past the YAML quoted scalar opening at `s[i]` (`'..''..'` or `"..\\".."`), or -1 if unterminated."""
    quote, n, j = s[i], len(s), i + 1
    while j < n:
        c = s[j]
        if quote == "'" and c == "'":
            if j + 1 < n and s[j + 1] == "'":
                j += 2
                continue
            return j + 1
        if quote == '"' and c == "\\":
            j += 2
            continue
        if quote == '"' and c == '"':
            return j + 1
        j += 1
    return -1


def _role_at(s: str, i: int) -> str:
    j = i
    while j < len(s) and "a" <= s[j] <= "z":
        j += 1
    return s[i:j] if j == len(s) or s[j] in ' \t:"[\'' else ""


def _plain_key_end(s: str, i: int) -> int:
    """End of `role "name" [state]...` starting at `s[i]` (the role must already be known to end there)."""
    j = i
    while j < len(s) and "a" <= s[j] <= "z":
        j += 1
    while j + 1 < len(s) and s[j] == " ":
        if s[j + 1] == '"':
            k = _quoted_end(s, j + 1)
            if k < 0:
                return len(s)
            j = k
        elif s[j + 1] == "[":
            k = s.find("]", j + 2)
            if k < 0:
                return len(s)
            j = k + 1
        else:
            break
    return j


def _entry(line: str) -> Optional[Tuple[int, int, str, int, bool]]:
    """(indent, key_start, role, key_end, has_inline_value) of a `- key[: value]` line, or None for anything else."""
    n = len(line)
    indent = 0
    while indent < n and line[indent] == " ":
        indent += 1
    if line[indent:indent + 1] != "-" or line[indent + 1:indent + 2] != " ":
        return None
    start = indent + 1
    while start < n and line[start] == " ":
        start += 1
    if start >= n:
        return None
    if line[start] in "'\"":
        end = _quoted_end(line, start)
        if end < 0:
            return None
        role = _role_at(line, start + 1)
    else:
        role = _role_at(line, start)
        end = _plain_key_end(line, start)
    rest = line[end:].lstrip(" \t")
    return indent, start, role, end, rest.startswith(":") and bool(rest[1:].strip())


def _is_property(line: str) -> bool:
    return line.lstrip(" ").startswith("- /")


def redact_typed_values(yaml_text: str) -> str:
    """`yaml_text` with the value removed from every input line of an accessibility snapshot, inline or nested."""
    out: List[str] = []
    frames: List[Tuple[int, str, List[int]]] = []   # open value-role entries: (indent, role, [child indent or -1])
    dropping = -1                                   # indent of a dropped child whose subtree is being skipped
    for line in yaml_text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        indent = len(bare) - len(bare.lstrip(" "))
        if not bare.strip():
            if dropping < 0:
                out.append(line)
            continue
        if dropping >= 0 and indent > dropping:
            continue
        dropping = -1
        while frames and frames[-1][0] >= indent:
            frames.pop()
        ent = _entry(bare)
        if frames:
            _, f_role, child = frames[-1]
            if child[0] < 0:
                child[0] = indent
            if indent <= child[0]:
                prop = _is_property(bare)
                if bare.lstrip(" ").startswith("- /value") or (not prop and (
                        ent is None or f_role in _FREE_TEXT_ROLES or ent[2] == "text")):
                    dropping = indent
                    continue
        if ent is not None and ent[2] in _VALUE_ROLES:
            frames.append((ent[0], ent[2], [-1]))
            # Cut at the key: the inline value and its colon go, and the dash is normalised to "- " so every
            # redacted line has one shape whatever the spacing it arrived with.
            line = " " * ent[0] + "- " + bare[ent[1]:ent[3]] + line[len(bare):]
        out.append(line)
    return "".join(out)


def _clean_item(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    out: Dict[str, Any] = {}
    for key, value in item.items():
        if not isinstance(key, str) or _DROP_KEY.search(key):
            continue
        if key not in INVENTORY_KEYS and not _DATA_KEY.match(key):
            continue
        if key == "box":
            if isinstance(value, dict) and all(isinstance(value.get(k), (int, float)) and not isinstance(value.get(k), bool)
                                               for k in _BOX_KEYS):
                out[key] = {k: value[k] for k in _BOX_KEYS}
            elif value is None:
                out[key] = None
        elif isinstance(value, bool):
            out[key] = value
        elif key in _BOOL_KEYS:
            continue
        elif isinstance(value, str):
            out[key] = value[:_FIELD_MAX]
    return out


def clean_inventory(items: Any) -> List[Dict[str, Any]]:
    """`items` (the probe's element list) keeping only allowlisted keys with plain values: a string (capped), a bool, or a
    numeric box. Anything else, and every key named like a value or a credential, is dropped."""
    return [c for c in (_clean_item(i) for i in (items if isinstance(items, list) else [])) if c is not None]


_URL_LINE = re.compile(r"^(\s*- /url:[ \t]*)([^\r\n]*)$", re.M)
_MAX_SCRUB_VALUES, _MIN_SCRUB_CHARS = 200, 3


def redact_urls(yaml_text: str) -> str:
    """`yaml_text` with the query and the fragment of every `- /url:` line cut (a link can carry a token or a code; the probe
    cuts them too, and the path is what a locator needs)."""
    return _URL_LINE.sub(lambda m: m.group(1) + re.split(r"[?#]", m.group(2), maxsplit=1)[0], yaml_text)


def scrub_values(text: str, values: List[str]) -> str:
    """`text` with every occurrence of each of `values` removed: what the page itself holds as typed or chosen text (an input's
    value, a textarea, a select, an editable region). The aria snapshot names roles it knows to be inputs; this catches the
    typed text it shows under a role it does not (a contenteditable region is a plain `generic` to it). Values shorter than
    three characters are left alone (they would eat ordinary words), and at most 200 are used."""
    wanted = []
    for value in values[:4 * _MAX_SCRUB_VALUES]:
        for piece in (value, *str(value).splitlines()):
            piece = piece.strip()[:_FIELD_MAX]
            if len(piece) >= _MIN_SCRUB_CHARS and piece not in wanted:
                wanted.append(piece)
    for piece in sorted(wanted[:_MAX_SCRUB_VALUES], key=len, reverse=True):
        text = text.replace(piece, "")
    return text


__all__ = ["INVENTORY_KEYS", "clean_inventory", "redact_typed_values", "redact_urls", "scrub_values"]
