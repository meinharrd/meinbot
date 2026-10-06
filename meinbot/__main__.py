"""python -m meinbot          run the Telegram bot
python -m meinbot ask "..."   one-off question from the terminal (owner scope, no Telegram)"""
import asyncio
import logging
import sys

from . import config


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = config.load()
    if len(sys.argv) > 2 and sys.argv[1] == "ask":
        asyncio.run(ask(cfg, " ".join(sys.argv[2:]), dry_run="--dry-run" in sys.argv))
        return
    if not cfg.telegram_token or not cfg.owner_id:
        sys.exit("Set TELEGRAM_TOKEN and OWNER_ID in .env")
    from .telegram_bot import Bot
    Bot(cfg).run()


async def ask(cfg, question: str, dry_run: bool = False):
    from .agent import Assistant
    from .compiler import ChatInfo
    from .memory import Scope
    a = Assistant(cfg)
    state = a.chats.load("cli", default_scope="owner", title="Terminal")
    history = [{"role": "user", "name": cfg.owner_name, "text": question.replace("--dry-run", "").strip()}]
    reply = await a.respond(state, Scope(owner=True), ChatInfo(f"terminal session with {cfg.owner_name}", cfg.owner_name),
                            history, dry_run=dry_run)
    print(reply.text)
    for n in reply.notes:
        print(f"[{n.kind}] {n.text} ({n.ref})")
    print(f"[usage] {reply.usage}", file=sys.stderr)


if __name__ == "__main__":
    main()
