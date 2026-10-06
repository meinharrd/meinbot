"""Model adapters. The rest of the bot speaks one neutral format:

    system: str
    messages: [{"role": "user", "content": str},
               {"role": "assistant", "content": str, "tool_calls": [ToolCall], "raw": <native>},
               {"role": "tool", "tool_call_id": str, "name": str, "content": str}]
    tools: [{"name", "description", "parameters": <JSON Schema>}]

Each adapter translates to its vendor's API. Adding a vendor means adding one
class here; memory, prompts and tools stay untouched.
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .config import ROOT, ModelRole

log = logging.getLogger(__name__)

STATE_DIR = ROOT / "state"


def _load_module(path: str):
    """Import a Python file by path (for optional local plugins)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(Path(path).stem, Path(path).expanduser())
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Result:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: object = None           # native assistant content, echoed back within a turn
    usage: dict = field(default_factory=dict)


# executor(tool_name, args) -> (result text, is_error)
Executor = Callable[[str, dict], Awaitable[tuple[str, bool]]]
MAX_TOOL_ROUNDS = 30


class Provider:
    def __init__(self, role: ModelRole):
        self.role = role

    @property
    def label(self) -> str:
        return f"{self.role.provider}/{self.role.model}"

    async def complete(self, system: str, messages: list[dict], tools: list[dict] | None = None,
                       max_tokens: int | None = None) -> Result:
        raise NotImplementedError

    async def run(self, system: str, messages: list[dict], tools: list[dict] | None,
                  execute: Executor) -> Result:
        """A full turn including tool calls. Adapters for agent runtimes that
        drive their own loop (Claude Code) override this instead of complete()."""
        messages = list(messages)
        usage: dict = {}
        text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            res = await self.complete(system, messages, tools)
            for k, v in res.usage.items():
                usage[k] = usage.get(k, 0) + v
            text = res.text or text
            if not res.tool_calls:
                return Result(text, usage=usage)
            messages.append({"role": "assistant", "content": res.text, "tool_calls": res.tool_calls,
                             "raw": res.raw})
            for call in res.tool_calls:
                out, is_err = await execute(call.name, call.args)
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                 "content": out, "is_error": is_err})
        # Out of steps: one last call asking for an answer from what was gathered.
        messages.append({"role": "user", "content": "You have run out of tool steps. Do not call tools; answer "
                         "now from what you found and say what is still unverified."})
        res = await self.complete(system, messages, tools)
        return Result(res.text or text or "(stopped after too many tool calls)", usage=usage)


def _merge_same_role(msgs: list[dict]) -> list[dict]:
    """Join consecutive plain-text messages of the same role."""
    out: list[dict] = []
    for m in msgs:
        if (out and out[-1]["role"] == m["role"] and isinstance(m["content"], str)
                and isinstance(out[-1]["content"], str)):
            out[-1] = {**out[-1], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(dict(m))
    return out


class AnthropicProvider(Provider):
    """Claude via the official Anthropic SDK."""

    def __init__(self, role: ModelRole):
        super().__init__(role)
        import anthropic
        key = os.environ.get(role.api_key_env or "ANTHROPIC_API_KEY")
        self.client = anthropic.AsyncAnthropic(api_key=key, base_url=role.base_url) if key \
            else anthropic.AsyncAnthropic(base_url=role.base_url)

    def _convert(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if m.get("is_error"):
                    block["is_error"] = True
                # all results for one assistant turn go into a single user message
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif m["role"] == "assistant" and m.get("raw") is not None:
                out.append({"role": "assistant", "content": m["raw"]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return _merge_same_role(out)

    async def complete(self, system, messages, tools=None, max_tokens=None) -> Result:
        x = self.role.extra
        params = dict(
            model=self.role.model,
            max_tokens=max_tokens or self.role.max_output_tokens,
            system=[{"type": "text", "text": system}],
            messages=self._convert(messages),
            cache_control={"type": "ephemeral"},   # automatic prompt caching
        )
        if tools:
            params["tools"] = [{"name": t["name"], "description": t["description"],
                                "input_schema": t["parameters"]} for t in tools]
        if x.get("effort"):
            params["output_config"] = {"effort": x["effort"]}
        if x.get("fallbacks", True):
            resp = await self.client.beta.messages.create(
                **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        else:
            resp = await self.client.messages.create(**params)

        if resp.stop_reason == "refusal":
            return Result(text="(The model declined to answer this.)")
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else json.loads(b.input))
                 for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason == "max_tokens" and not calls:
            text += "\n\n(cut off: output limit reached)"
        u = resp.usage
        usage = {"in": u.input_tokens, "out": u.output_tokens,
                 "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0}
        return Result(text=text, tool_calls=calls, raw=resp.content, usage=usage)


class OpenAICompatProvider(Provider):
    """Any OpenAI-compatible chat-completions endpoint: OpenAI, OpenRouter,
    Gemini (OpenAI endpoint), Mistral, Groq, Together, Ollama, vLLM, LM Studio..."""

    def __init__(self, role: ModelRole):
        super().__init__(role)
        import openai
        key = os.environ.get(role.api_key_env or "OPENAI_API_KEY") or "none"
        self.client = openai.AsyncOpenAI(api_key=key, base_url=role.base_url)

    def _convert(self, system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                out.append({
                    "role": "assistant", "content": m.get("content") or None,
                    "tool_calls": [{"id": c.id, "type": "function",
                                    "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                   for c in m["tool_calls"]],
                })
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return _merge_same_role(out)

    async def complete(self, system, messages, tools=None, max_tokens=None) -> Result:
        x = self.role.extra
        params = dict(model=self.role.model, messages=self._convert(system, messages))
        params[x.get("max_tokens_param", "max_tokens")] = max_tokens or self.role.max_output_tokens
        if tools:
            params["tools"] = [{"type": "function", "function": t} for t in tools]
        params.update(x.get("request", {}))
        resp = await self.client.chat.completions.create(**params)
        msg = resp.choices[0].message
        calls = []
        for c in msg.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": c.function.arguments}
            calls.append(ToolCall(c.id, c.function.name, args))
        usage = {}
        if resp.usage:
            usage = {"in": resp.usage.prompt_tokens, "out": resp.usage.completion_tokens}
        return Result(text=msg.content or "", tool_calls=calls, usage=usage)


class ClaudeCodeProvider(Provider):
    """Claude through the Claude Agent SDK, i.e. on the machine's Claude Code
    login (subscription) instead of an API key. Built-in tools, settings files
    and CLAUDE.md are all switched off: the model sees only our system prompt
    and our memory tools, exactly like with the other adapters.

    Optional `extra.router`: path to a module that picks among several logins
    (functions pick, env_for, classify_limit, mark_limited, next_account).
    Without it, the host login is used."""

    def __init__(self, role: ModelRole):
        super().__init__(role)
        self.cwd = Path(role.extra.get("cwd", STATE_DIR / "claude-code"))
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.router = _load_module(role.extra["router"]) if role.extra.get("router") else None

    @staticmethod
    def _prompt(messages: list[dict]) -> str:
        """The SDK takes one prompt per query, so earlier turns become a transcript."""
        *earlier, last = messages
        if not earlier:
            return last["content"]
        lines = [f"[{'User' if m['role'] == 'user' else 'You'}]: {m['content']}" for m in earlier]
        return ("<conversation_so_far>\n" + "\n\n".join(lines) + "\n</conversation_so_far>\n\n"
                + last["content"])

    async def complete(self, system, messages, tools=None, max_tokens=None) -> Result:
        async def no_tools(name, args):
            return "tools unavailable", True
        return await self.run(system, messages, None, no_tools)

    async def run(self, system, messages, tools, execute) -> Result:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKError, ResultMessage,
                                      SdkMcpTool, TextBlock, create_sdk_mcp_server, query)
        router = self.router
        findings: list[str] = []          # tool results, for a wrap-up if the step limit is hit

        def handler(name):
            async def h(args):
                out, is_err = await execute(name, args)
                findings.append(f"{name} {json.dumps(args, ensure_ascii=False)}:\n{out[:2500]}")
                return {"content": [{"type": "text", "text": out}], "isError": is_err}
            return h

        sdk_tools = [SdkMcpTool(t["name"], t["description"], t["parameters"], handler(t["name"]))
                     for t in tools or []]
        server = create_sdk_mcp_server(name="bot", version="1.0.0", tools=sdk_tools)
        max_turns = self.role.extra.get("max_turns", MAX_TOOL_ROUNDS)

        async def attempt(prompt: str, with_tools: bool, turns: int):
            """One query; returns (text, result, account, hit the step limit)."""
            tried: set[str] = set()
            while True:
                acct = router.pick(None, self.role.model) if router else None
                env = router.env_for(acct) if acct else {}
                opts = ClaudeAgentOptions(
                    system_prompt=system, model=self.role.model, tools=[],
                    allowed_tools=[f"mcp__bot__{t['name']}" for t in tools or []] if with_tools else [],
                    mcp_servers={"bot": server} if with_tools else {}, setting_sources=[],
                    cwd=str(self.cwd), env=env, permission_mode="bypassPermissions",
                    max_turns=turns, effort=self.role.extra.get("effort"),
                )
                texts, result, exhausted = [], None, False
                try:
                    async for msg in query(prompt=prompt, options=opts):
                        if isinstance(msg, AssistantMessage):
                            texts.append("".join(b.text for b in msg.content if isinstance(b, TextBlock)))
                        elif isinstance(msg, ResultMessage):
                            result = msg
                except ClaudeSDKError as e:
                    if "maximum number of turns" not in str(e):
                        raise
                    exhausted = True
                if result is not None and result.subtype == "error_max_turns":
                    exhausted = True
                text = (result.result if result and result.result else (texts[-1] if texts else "")).strip()
                failed = result is None or result.is_error or (result.subtype or "success") != "success"
                hit = router.classify_limit(text, self.role.model) if failed and router and not exhausted else None
                if hit and acct:
                    router.mark_limited(acct.name, hit[2] or time.time() + 1800, text)
                    tried.add(acct.name)
                    nxt = router.next_account(None, self.role.model, acct.name)
                    if nxt and nxt not in tried:
                        log.info("account %s limited, retrying on %s", acct.name, nxt)
                        continue
                return text, result, acct, exhausted

        prompt = self._prompt(messages)
        text, result, acct, exhausted = await attempt(prompt, True, max_turns)
        if exhausted:
            # Out of steps: answer from what was gathered instead of failing.
            log.info("step limit (%d) reached; wrapping up from %d tool results", max_turns, len(findings))
            notes, size = [], 0
            for f in reversed(findings):
                if size + len(f) > 40000:
                    break
                notes.insert(0, f)
                size += len(f)
            wrap = (prompt + "\n\n<work_so_far>\nYou already used these tools for this message:\n\n"
                    + "\n\n".join(notes) + "\n</work_so_far>\n\nYou have run out of tool steps. Answer now "
                    "from what you found, say clearly what is still unverified, and suggest what to look "
                    "up next if it matters.")
            text, result, acct, _ = await attempt(wrap, False, 2)
        failed = result is None or result.is_error or (result.subtype or "success") != "success"
        if failed and not text:
            text = f"(Claude Code error: {result.subtype if result else 'no result'})"
        u = (result.usage or {}) if result else {}
        usage = {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
                 "cache_read": u.get("cache_read_input_tokens", 0),
                 "account": acct.name if acct else "host"}
        return Result(text=text, usage=usage)


PROVIDERS = {"anthropic": AnthropicProvider, "openai": OpenAICompatProvider,
             "claude_code": ClaudeCodeProvider}


def make(role: ModelRole) -> Provider:
    try:
        return PROVIDERS[role.provider](role)
    except KeyError:
        raise ValueError(f"unknown provider {role.provider!r}; known: {', '.join(PROVIDERS)}")
