"""Offline tests: memory store, scopes, search, compiler, tool loop (fake model)."""
import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from meinbot import compiler, providers, tools
from meinbot.agent import Assistant
from meinbot.chats import ChatState, Transcripts
from meinbot.config import Config, ModelRole
from meinbot.memory import MemoryError_, Scope, Store
from meinbot.search import Index

SRC = Path(__file__).resolve().parent.parent / "examples" / "memory"


@pytest.fixture
def mem(tmp_path):
    root = tmp_path / "mem"
    shutil.copytree(SRC, root, ignore=shutil.ignore_patterns(".git", "transcripts"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "init"], check=True)
    return root


def test_scopes(mem):
    s = Store(mem)
    owner, shared = Scope(owner=True), Scope(owner=False, shared_files={"topics/garden-project.md"})
    vault = s.get("vault/identity.md")
    assert not owner.can_inject(vault) and owner.can_read(vault)
    assert not shared.can_read(vault)
    assert shared.can_read(s.get("topics/garden-project.md"))
    assert not shared.can_read(s.get("core.md"))
    assert "vault/identity.md" in s.index(owner)
    assert "vault" not in s.index(shared) and "work.md" not in s.index(shared)


def test_search(mem):
    s = Store(mem)
    idx = Index(s)
    hits = idx.search("How big is the garden plot?", Scope(owner=True).can_inject)
    assert hits and hits[0].path == "topics/garden-project.md"
    # vault never comes up in automatic retrieval, but does for explicit search
    assert all(not h.path.startswith("vault/") for h in idx.search("date of birth", Scope(owner=True).can_inject))
    assert any(h.path.startswith("vault/") for h in idx.search("date of birth", Scope(owner=True).can_read))
    assert idx.search("Northwind day rate", Scope(owner=False).can_inject) == []


def test_apply_ops(mem):
    s = Store(mem)
    s.apply({"op": "add", "path": "topics/garden-project", "text": "Opened softly in Nov 2026.", "section": "Status"})
    raw = s.read_raw("topics/garden-project.md")
    assert "## Status\n- Opened softly in Nov 2026. [U]" in raw
    s.apply({"op": "supersede", "path": "topics/garden-project.md", "old_text": "Opened softly",
             "text": "Opening delayed to Jan 2027.", "reason": "contractor"})
    raw = s.read_raw("topics/garden-project.md")
    assert "~~Opened softly in Nov 2026." in raw and "Opening delayed to Jan 2027. [U]" in raw
    s.apply({"op": "create", "path": "people/jane-doe.md", "text": "Architect for the garden shed.",
             "sensitivity": "personal", "description": "Garden shed architect"})
    assert s.get("people/jane-doe.md").level == 1
    with pytest.raises(MemoryError_):
        s.apply({"op": "add", "path": "charter.md", "text": "x"})
    with pytest.raises(MemoryError_):
        s.apply({"op": "create", "path": "../evil.md", "text": "x"})
    with pytest.raises(MemoryError_):
        s.apply({"op": "supersede", "path": "topics/garden-project.md", "old_text": "nonexistent", "text": "x"})


def test_compiler(mem):
    s = Store(mem)
    idx = Index(s)
    state = ChatState(key="t", scope="owner", bind=["topics/garden-project.md"], summary="- talked about saunas")
    hist = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"},
            {"role": "user", "text": "what is my day rate at Northwind?"}]
    system, msgs = compiler.build(s, idx, state, Scope(owner=True), compiler.ChatInfo("test"),
                                  hist, budget=60000, timezone="Europe/Berlin", recent_turns=30)
    assert system.startswith("# Charter")
    assert "Core profile" in system and "Pinned for this chat: topics/garden-project.md" in system
    assert "talked about saunas" in system
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert "topics/work.md" in msgs[-1]["content"] and "<now>" in msgs[-1]["content"]
    # shared chat: nothing private leaks
    system, msgs = compiler.build(s, idx, ChatState(key="g"), Scope(owner=False), compiler.ChatInfo("g"),
                                  hist, budget=60000, timezone="Europe/Berlin", recent_turns=30)
    assert "Core profile" not in system and "620" not in system + msgs[-1]["content"]


class FakeProvider(providers.Provider):
    """Calls memory_update once, then answers."""
    def __init__(self):
        super().__init__(ModelRole(provider="fake", model="fake"))
        self.calls = []

    async def complete(self, system, messages, tools=None, max_tokens=None):
        self.calls.append(messages)
        if messages[-1]["role"] == "user":
            return providers.Result("", [providers.ToolCall("c1", "memory_update", {
                "op": "add", "path": "topics/work.md", "section": "Clients",
                "text": "Signed a second client, Contoso, in Oct 2026."})])
        return providers.Result("Noted.")


def test_tool_loop(mem, tmp_path):
    role = ModelRole(provider="anthropic", model="x")
    cfg = Config("", "Robin", 1, mem, tmp_path / "state", "Europe/Berlin", role, role, 30, 40, 30, 600, "private")
    a = Assistant.__new__(Assistant)
    a.cfg, a.store = cfg, Store(mem)
    a.index = Index(a.store)
    from meinbot.gitsync import GitSync
    a.git = GitSync(mem)
    subprocess.run(["git", "-C", str(mem), "config", "user.email", "t@t"])
    subprocess.run(["git", "-C", str(mem), "config", "user.name", "t"])
    a.pending = tools.Pending(cfg.state_dir)
    a.chat_model = FakeProvider()
    state = ChatState(key="dm", scope="owner")
    hist = [{"role": "user", "text": "I signed Contoso today"}]
    reply = asyncio.run(a.respond(state, Scope(owner=True), compiler.ChatInfo("dm"), hist))
    assert reply.text == "Noted."
    assert reply.notes[0].kind == "saved" and reply.notes[0].ref
    assert "Signed a second client, Contoso" in a.store.read_raw("topics/work.md")
    # shared chat: same update is queued, not applied
    a.chat_model = FakeProvider()
    reply = asyncio.run(a.respond(ChatState(key="g"), Scope(owner=False), compiler.ChatInfo("g"), hist))
    assert reply.notes[0].kind == "pending"
    assert len(a.pending.items) == 1


def test_transcripts(tmp_path):
    t = Transcripts(tmp_path)
    t.append("k", "user", "M", "hello")
    t.append("k", "assistant", "assistant", "hi")
    assert [e["text"] for e in t.entries("k")] == ["hello", "hi"]


def test_web_refuses_private_addresses():
    from meinbot import web
    for url in ("http://127.0.0.1:8080/", "http://localhost/", "http://169.254.169.254/latest/meta-data",
                "http://10.0.0.1/", "file:///etc/passwd"):
        with pytest.raises(web.WebError):
            asyncio.run(web.fetch(url))
