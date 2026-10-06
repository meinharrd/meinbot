"""Per-chat state and transcripts, both stored in the memory repo.

chats/<key>.md holds settings (scope, bound topics, shared files) in
frontmatter and a rolling summary in the body, so they sync like any memory.
transcripts/YYYY-MM/<key>.jsonl holds every message: owning the raw
conversations is what allows re-extracting memory later with a better model.
"""
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .memory import Scope, Store


def chat_key(chat_id: int, thread_id: int | None = None) -> str:
    key = f"tg{chat_id}".replace("-", "m")
    return f"{key}-t{thread_id}" if thread_id else key


@dataclass
class ChatState:
    key: str
    title: str = ""
    scope: str = "shared"            # "owner" | "shared"; groups default to shared
    bind: list[str] = field(default_factory=list)    # files pinned into context
    share: list[str] = field(default_factory=list)   # files visible in a shared chat
    summary_until: str = ""          # ts of the last transcript entry in the summary
    voice: str = "auto"              # voice replies: off | auto (to voice notes) | always
    summary: str = ""

    @property
    def path(self) -> str:
        return f"chats/{self.key}.md"

    def render(self) -> str:
        meta = {
            "title": self.title or self.key,
            "description": f"Telegram chat {self.key}",
            "sensitivity": "private" if self.scope == "owner" else "personal",
            "kind": "chat",
            "scope": self.scope,
            "bind": self.bind,
            "share": self.share,
            "summary_until": self.summary_until,
            "voice": self.voice,
        }
        fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
        body = f"# {self.title or self.key}\n\n## Summary\n{self.summary.strip()}\n"
        return f"---\n{fm}\n---\n{body}"


class Chats:
    def __init__(self, store: Store):
        self.store = store

    def load(self, key: str, default_scope: str = "shared", title: str = "") -> ChatState:
        doc = self.store.get(f"chats/{key}.md")
        if not doc:
            return ChatState(key=key, scope=default_scope, title=title)
        m = doc.meta
        summary = doc.body.split("## Summary", 1)[1].strip() if "## Summary" in doc.body else ""
        return ChatState(
            key=key, title=m.get("title", title), scope=m.get("scope", default_scope),
            bind=list(m.get("bind") or []), share=list(m.get("share") or []),
            summary_until=str(m.get("summary_until") or ""), summary=summary,
            voice=m.get("voice", "auto"),
        )

    def save(self, state: ChatState):
        self.store.write_raw(state.path, state.render())

    def scope_for(self, state: ChatState, group: ChatState | None = None) -> Scope:
        """Topic threads inherit scope and shares from their group."""
        base = group or state
        return Scope(owner=base.scope == "owner", shared_files=set(base.share) | set(state.share))


class Transcripts:
    def __init__(self, root: Path):
        self.root = root / "transcripts"

    def append(self, key: str, role: str, name: str, text: str, **extra):
        now = dt.datetime.now(dt.timezone.utc)
        p = self.root / now.strftime("%Y-%m") / f"{key}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": now.isoformat(timespec="seconds"), "role": role, "name": name, "text": text, **extra}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def entries(self, key: str, months: int = 2) -> list[dict]:
        """Entries from the last few monthly files, oldest first."""
        files = sorted(self.root.glob(f"*/{key}.jsonl"))[-months:]
        out = []
        for f in files:
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

