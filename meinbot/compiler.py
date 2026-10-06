"""The context compiler: assembles what the model sees, identically for every vendor.

Layout (stable parts first so any vendor's prompt caching can reuse them):

  system:   charter · core profile · memory index · pinned files · chat info · chat summary
  messages: recent turns ... final user turn = retrieved excerpts + time + message

Everything is plain text. A model that never saw this bot before gets all it
needs to act like it always has: who it is, who its owner is, what it knows,
and where to look for more.
"""
import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .chats import ChatState
from .memory import Scope, Store
from .search import Index


def tokens(text: str) -> int:
    """Rough, vendor-neutral token estimate."""
    return len(text) // 3 + 1


@dataclass
class ChatInfo:
    description: str          # e.g. "direct chat with Robin" / "group 'Projects', topic 'Garden'"
    owner_name: str = "the owner"


def _speaker(entry: dict, scope: Scope) -> str:
    text = entry["text"]
    if not scope.owner or entry.get("is_other"):
        return f"{entry.get('name') or 'someone'}: {text}"
    return text


def build(store: Store, index: Index, state: ChatState, scope: Scope, info: ChatInfo,
          history: list[dict], budget: int, timezone: str, recent_turns: int) -> tuple[str, list[dict]]:
    parts = [store.charter]

    core = store.core
    if core and scope.can_inject(core):
        parts.append("# Core profile\n" + core.body.strip())

    idx = store.index(scope)
    if idx:
        parts.append("# Memory index (read files with memory_read)\n" + idx)

    pinned_paths = []
    pin_budget = budget // 4
    for path in state.bind:
        doc = store.get(path)
        if not doc or not scope.can_inject(doc):
            continue
        body = doc.body.strip()
        if tokens(body) > pin_budget:
            body = body[: pin_budget * 3] + "\n[... truncated; use memory_read for the rest]"
        pin_budget -= tokens(body)
        pinned_paths.append(doc.path)
        parts.append(f"# Pinned for this chat: {doc.path}\n{body}")
        if pin_budget <= 0:
            break

    scope_text = (f"private to {info.owner_name}" if scope.owner
                  else "shared with other people; you only see memory explicitly shared with this chat")
    parts.append(f"# This chat\nYou are in the {info.description}. This chat is {scope_text}.")
    if state.summary:
        parts.append("# Earlier in this chat (summary)\n" + state.summary)

    system = "\n\n".join(p for p in parts if p)

    # --- retrieval for the latest message ------------------------------------
    user_entries = [e for e in history if e["role"] == "user"]
    latest = user_entries[-1]["text"] if user_entries else ""
    query = " ".join(e["text"] for e in user_entries[-2:])
    exclude = set(pinned_paths) | {"core.md"}
    hits = index.search(query, scope.can_inject, limit=8, exclude=exclude)
    excerpt_budget = budget // 8
    excerpts = []
    for h in hits:
        block = f"[{h.path} › {h.heading}]\n{h.text}"
        if tokens(block) > excerpt_budget:
            break
        excerpt_budget -= tokens(block)
        excerpts.append(block)

    now = dt.datetime.now(ZoneInfo(timezone))
    final_parts = []
    if excerpts:
        final_parts.append("<memory_excerpts>\n" + "\n\n".join(excerpts) + "\n</memory_excerpts>")
    final_parts.append(f"<now>{now:%A %Y-%m-%d %H:%M} ({timezone})</now>")
    final_parts.append(_speaker(user_entries[-1], scope) if user_entries else latest)
    final = "\n\n".join(final_parts)

    # --- recent turns, newest first until the budget is used ------------------
    remaining = budget - tokens(system) - tokens(final)
    turns: list[dict] = []
    earlier = history[:-1] if history and history[-1]["role"] == "user" else history
    for e in reversed(earlier[-recent_turns:]):
        content = _speaker(e, scope) if e["role"] == "user" else e["text"]
        cost = tokens(content)
        if cost > remaining:
            break
        remaining -= cost
        turns.append({"role": "user" if e["role"] == "user" else "assistant", "content": content})
    turns.reverse()
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return system, turns + [{"role": "user", "content": final}]
