"""
===============================================================================
Test Support (an in-process Voyageur service with a scripted model)
===============================================================================
The end-to-end and contract tests run against the real Voyageur service (the `voyageur` dev dependency), in this process, with
a model that answers from a script: no model endpoint, no network beyond 127.0.0.1, no GPU. The service is skipped-for
(importorskip) where it cannot be installed (it needs Python 3.12).

Features:
    ✓ ScriptedModel: answers each model call from a list of {"reply", "usage"} and records every prompt
    ✓ build_app(): the service's FastAPI app on an in-memory record
    ✓ ServiceThread: that app served by uvicorn on a free 127.0.0.1 port, for tests that run a pytest child process

Environment Variables:
    None.

Author: PMAC
===============================================================================
"""
import socket
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("voyageur", reason="the voyageur service is not installed (pip install -e .[dev], Python 3.12)")
uvicorn = pytest.importorskip("uvicorn")

from voyageur.app import create_app  # noqa: E402
from voyageur.config import ServiceSettings  # noqa: E402
from voyageur.core import HealSession  # noqa: E402
from voyageur.llm import Endpoint, Usage  # noqa: E402
from voyageur.store import Store  # noqa: E402
from voyageur.transport import CallResult  # noqa: E402

ENDPOINT = Endpoint(base_url="http://localhost:11434/v1", model="scripted-test-model")


class ScriptedModel:
    """A model that answers from the script: {"reply": text or None, "usage": [input, output]} per call."""

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.script = list(script)
        self.prompts: List[str] = []

    def call(self, endpoint: Any, prompt: str, images: Any = (), heal_id: str = "") -> CallResult:
        self.prompts.append(prompt)
        step = self.script.pop(0) if self.script else {"reply": None, "usage": [0, 0]}
        text = step.get("reply")
        return CallResult(text, "" if text else "no answer", Usage(*step.get("usage", [0, 0])), 1, True)


def pick(ref: str, usage: Optional[List[int]] = None) -> Dict[str, Any]:
    """One scripted model answer that names the element `ref` (e0, e1, ...)."""
    return {"reply": '{"ref": "%s", "why": "scripted pick"}' % ref, "usage": usage or [100, 8]}


def build_app(model: ScriptedModel, **settings: Any) -> Any:
    return create_app(HealSession(ServiceSettings(retention_days=14, **settings), Store(":memory:", 14), model, ENDPOINT), None)


class ServiceThread:
    """The service on 127.0.0.1:<free port> in a thread, until the `with` block ends."""

    def __init__(self, app: Any) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "ServiceThread":
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("the in-process service did not start")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class RuleModel(ScriptedModel):
    """A model that picks by what the prompt shows: the first rule whose text is in the prompt names the ref to answer. For tests
    that heal several failures in any order (parallel workers), where a plain script would hand a pick to the wrong test."""

    def __init__(self, rules: List[Any]) -> None:
        super().__init__([])
        self.rules = rules

    def call(self, endpoint: Any, prompt: str, images: Any = (), heal_id: str = "") -> CallResult:
        self.prompts.append(prompt)
        for needle, ref in self.rules:
            if needle in prompt:
                return CallResult('{"ref": "%s", "why": "rule pick"}' % ref, "", Usage(100, 8), 1, True)
        return CallResult(None, "no answer", Usage(0, 0), 1, True)
