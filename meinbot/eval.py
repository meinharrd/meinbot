"""Check that the current model config answers from memory correctly.

    python -m meinbot.eval            # all questions
    python -m meinbot.eval -v         # show answers

Questions live with the memory they test, in <memory_dir>/evals.yaml
(see examples/memory/evals.yaml). Runs in owner scope, dry run (nothing is
written). Use it before switching the chat model to another vendor: if it
passes, the switch is safe.
"""
import asyncio
import sys

import yaml

from . import config
from .agent import Assistant
from .compiler import ChatInfo
from .memory import Scope


def check(answer: str, case: dict) -> list[str]:
    a = answer.lower()
    problems = [f"missing one of {grp}" for grp in case.get("expect", [])
                if not any(str(alt).lower() in a for alt in grp)]
    problems += [f"contains {bad!r}" for bad in case.get("forbid", []) if bad.lower() in a]
    return problems


async def run(verbose: bool):
    cfg = config.load()
    a = Assistant(cfg)
    path = cfg.memory_dir / "evals.yaml"
    if not path.exists():
        sys.exit(f"no eval questions at {path} (see examples/memory/evals.yaml)")
    cases = yaml.safe_load(path.read_text())
    state = a.chats.load("eval", default_scope="owner", title="Eval")
    passed = 0
    for case in cases:
        hist = [{"role": "user", "name": cfg.owner_name, "text": case["q"]}]
        reply = await a.respond(state, Scope(owner=True), ChatInfo(f"evaluation run with {cfg.owner_name}", cfg.owner_name),
                                hist, dry_run=True)
        problems = check(reply.text, case)
        passed += not problems
        print(f"{'PASS' if not problems else 'FAIL'}  {case['q']}" + (f"  -> {'; '.join(problems)}" if problems else ""))
        if verbose or problems:
            print("      " + reply.text.replace("\n", "\n      "))
    print(f"\n{passed}/{len(cases)} passed with {a.chat_model.label}")
    return passed == len(cases)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run("-v" in sys.argv)) else 1)
