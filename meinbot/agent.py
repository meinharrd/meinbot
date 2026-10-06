"""One conversational turn: compile context, run the tool loop, return text."""
import logging
from dataclasses import dataclass

from . import compiler, providers, tools
from .chats import Chats, ChatState, Transcripts
from .config import Config
from .gitsync import GitSync
from .memory import Scope, Store
from .search import Index

log = logging.getLogger(__name__)

SUMMARY_PROMPT = """You maintain the rolling summary of one chat for a personal assistant's memory.
Merge the previous summary and the new messages into an updated summary of at most 300 words.
Keep decisions, open tasks, commitments, names, dates and figures. Drop small talk.
Write plain Markdown bullets, newest developments last. Output only the summary."""


@dataclass
class Reply:
    text: str
    notes: list[tools.Note]
    usage: dict


class Assistant:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.memory_dir)
        self.index = Index(self.store)
        self.git = GitSync(cfg.memory_dir, cfg.push_delay)
        self.pending = tools.Pending(cfg.state_dir)
        self.chats = Chats(self.store)
        self.transcripts = Transcripts(cfg.memory_dir)
        self.chat_model = providers.make(cfg.chat)
        self.memory_model = providers.make(cfg.memory)

    async def respond(self, state: ChatState, scope: Scope, info: compiler.ChatInfo,
                      history: list[dict], dry_run: bool = False) -> Reply:
        role = self.cfg.chat
        system, messages = compiler.build(
            self.store, self.index, state, scope, info, history,
            budget=role.context_tokens, timezone=self.cfg.timezone, recent_turns=self.cfg.recent_turns)
        ctx = tools.ToolContext(self.store, self.index, self.git, self.pending, scope, state.key,
                                auto_apply_max=self.cfg.auto_apply_max, dry_run=dry_run)
        tool_defs = tools.TOOLS if role.tools else None

        async def execute(name: str, args: dict):
            out, is_err = await tools.execute(name, args, ctx)
            log.info("tool %s %s -> %s", name, args, out[:120])
            return out, is_err

        res = await self.chat_model.run(system, messages, tool_defs, execute)
        text, usage = res.text, res.usage
        return Reply(text.strip(), ctx.notes, usage)

    async def apply_pending(self, pid: str) -> str:
        item = self.pending.pop(pid)
        if not item:
            return "Already handled."
        desc = self.store.apply(item["update"])
        sha = await self.git.commit(f"memory: {desc[:200]}")
        return f"Saved: {desc} ({sha})"

    async def maybe_summarize(self, state: ChatState):
        """Fold old messages into the chat's rolling summary."""
        entries = self.transcripts.entries(state.key, months=3)
        fresh = [e for e in entries if e["ts"] > state.summary_until]
        if len(fresh) < self.cfg.recent_turns + self.cfg.summarize_every:
            return
        batch = fresh[: -self.cfg.recent_turns]
        lines = "\n".join(f"[{e['ts'][:16]}] {e.get('name') or e['role']}: {e['text']}" for e in batch)
        prompt = f"Previous summary:\n{state.summary or '(none)'}\n\nNew messages:\n{lines}"
        res = await self.memory_model.complete(SUMMARY_PROMPT, [{"role": "user", "content": prompt}],
                                               max_tokens=2000)
        if not res.text.strip():
            return
        state = self.chats.load(state.key)       # reload: settings may have changed meanwhile
        state.summary = res.text.strip()
        state.summary_until = batch[-1]["ts"]
        self.chats.save(state)
        await self.git.commit(f"chat summary: {state.key}")
