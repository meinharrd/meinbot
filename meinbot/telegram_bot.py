"""Telegram front end: DM with the owner, owner groups with topics, shared groups."""
import asyncio
import html
import logging
import tempfile
from pathlib import Path
from collections import defaultdict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import audio
from .agent import Assistant
from .chats import ChatState, chat_key
from .compiler import ChatInfo
from .config import Config
from .formatting import md_to_telegram_html, split_message

log = logging.getLogger(__name__)

HELP = """Commands (owner only):
/register owner|shared – activate this group (owner = only you; shared = with others)
/scope owner|shared – change a group's scope
/bind <file> … – pin memory files into this chat/topic (e.g. topics/garden-project.md)
/unbind [file] – remove one or all pinned files
/share <file> / /unshare <file> – make a file visible in a shared chat
/chatinfo – settings of this chat
/memory [query] – show the memory index or search it
/show <file> – show a memory file
/log – recent memory changes
/undo <sha> – revert a memory change
/sync – push/pull the encrypted memory repo now
/model – show the models in use
/voice off|auto|always – voice replies (auto = answer voice notes with voice)

Voice notes are transcribed locally and answered like text."""


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.a = Assistant(cfg)
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.app = (Application.builder().token(cfg.telegram_token)
                    .post_init(self._post_init).post_shutdown(self._post_shutdown).build())
        owner = filters.User(user_id=cfg.owner_id)
        cmds = {
            "register": self.cmd_register, "scope": self.cmd_scope, "bind": self.cmd_bind,
            "unbind": self.cmd_unbind, "share": self.cmd_share, "unshare": self.cmd_unshare,
            "chatinfo": self.cmd_chatinfo, "memory": self.cmd_memory, "show": self.cmd_show,
            "log": self.cmd_log, "undo": self.cmd_undo, "sync": self.cmd_sync, "model": self.cmd_model,
            "voice": self.cmd_voice,
        }
        for name, fn in cmds.items():
            self.app.add_handler(CommandHandler(name, fn, filters=owner))
        self.app.add_handler(CommandHandler(["start", "help"], self.cmd_help, filters=owner))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        self.app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, self.on_message))
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.on_voice))

    def run(self):
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

    # ---- lifecycle --------------------------------------------------------

    async def _post_init(self, app: Application):
        try:
            if await self.a.git.pull():
                self.a.store.reload()
        except Exception:
            log.exception("initial pull failed")
        self._pull_task = asyncio.get_running_loop().create_task(self._pull_loop())
        await app.bot.set_my_commands([(c.split(' ')[0].lstrip('/'), c.split('–', 1)[1].strip()[:60])
                                       for c in HELP.splitlines()[1:] if c.startswith('/') and '–' in c])

    async def _post_shutdown(self, app: Application):
        try:
            await self.a.git.sync()
        except Exception:
            log.exception("final sync failed")

    async def _pull_loop(self):
        while True:
            await asyncio.sleep(self.cfg.pull_interval)
            try:
                if await self.a.git.pull():
                    self.a.store.reload()
                    log.info("memory updated from remote")
            except Exception as e:
                log.warning("pull failed: %s", e)

    # ---- chat resolution ----------------------------------------------------

    def _resolve(self, msg: Message) -> tuple[ChatState, ChatState | None, ChatInfo] | None:
        """Load chat state; None if the bot should not operate in this chat."""
        chat = msg.chat
        if chat.type == ChatType.PRIVATE:
            if chat.id != self.cfg.owner_id:
                return None
            key = chat_key(chat.id)
            return self.a.chats.load(key, default_scope="owner", title="Direct chat"), None, \
                ChatInfo(f"direct chat with {self.cfg.owner_name}", self.cfg.owner_name)
        group = self.a.chats.load(chat_key(chat.id), title=chat.title or "")
        if not self.a.store.get(group.path):
            return None                      # unregistered group: stay silent
        thread = msg.message_thread_id if msg.is_topic_message else None
        if not thread:
            return group, None, ChatInfo(f"group chat '{chat.title}'", self.cfg.owner_name)
        topic_name = ""
        r = msg.reply_to_message
        if r and r.forum_topic_created:
            topic_name = r.forum_topic_created.name
        state = self.a.chats.load(chat_key(chat.id, thread), title=topic_name)
        if topic_name and state.title != topic_name:
            state.title = topic_name
        return state, group, ChatInfo(f"group '{chat.title}', topic '{state.title or thread}'", self.cfg.owner_name)

    def _is_addressed(self, msg: Message, bot_username: str, bot_id: int) -> bool:
        text = msg.text or msg.caption or ""
        if f"@{bot_username}".lower() in text.lower():
            return True
        r = msg.reply_to_message
        return bool(r and r.from_user and r.from_user.id == bot_id and not r.forum_topic_created)

    # ---- messages ------------------------------------------------------------

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if msg:
            await self._handle(update, context, msg.text or msg.caption or "")

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if not msg or not self._resolve(msg):
            return
        # Only transcribe what the bot would answer: DMs, owner chats, or replies to the bot.
        state, group, _ = self._resolve(msg)
        is_owner = update.effective_user and update.effective_user.id == self.cfg.owner_id
        if not (self.a.chats.scope_for(state, group).owner and is_owner) and not \
                self._is_addressed(msg, context.bot.username, context.bot.id):
            return
        media = msg.voice or msg.audio
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "in.oga"
            await (await media.get_file()).download_to_drive(path)
            try:
                text, lang = await audio.transcribe(str(path))
            except Exception as e:
                log.exception("transcription failed")
                await msg.reply_text(f"Could not transcribe: {e}")
                return
        if not text:
            await msg.reply_text("(empty voice note)")
            return
        await msg.reply_text(f"🎙 {text}", do_quote=False, disable_notification=True)
        await self._handle(update, context, text, voice_lang=lang)

    async def _handle(self, update: Update, context, text: str, voice_lang: str | None = None):
        msg = update.effective_message
        user = update.effective_user
        if not user:
            return
        resolved = self._resolve(msg)
        if not resolved:
            return
        state, group, info = resolved
        scope = self.a.chats.scope_for(state, group)
        is_owner = user.id == self.cfg.owner_id
        self.a.transcripts.append(state.key, "user", user.full_name, text, uid=user.id,
                                  is_other=not is_owner, **({"voice": True} if voice_lang else {}))

        private_dm = msg.chat.type == ChatType.PRIVATE
        addressed = voice_lang is not None or self._is_addressed(msg, context.bot.username, context.bot.id)
        if not (private_dm or (scope.owner and is_owner) or addressed):
            return

        async with self.locks[state.key]:
            history = self.a.transcripts.entries(state.key)[-(self.cfg.recent_turns + 1):]
            typing = asyncio.create_task(self._typing(msg))
            try:
                reply = await self.a.respond(state, scope, info, history)
            except Exception as e:
                log.exception("respond failed")
                await msg.reply_text(f"Something went wrong: {e}")
                return
            finally:
                typing.cancel()
            log.info("%s usage %s", state.key, reply.usage)
            if reply.text:
                await self._send(msg, reply.text)
                self.a.transcripts.append(state.key, "assistant", "assistant", reply.text)
                if state.voice == "always" or (state.voice == "auto" and voice_lang):
                    await self._send_voice(msg, reply.text, voice_lang)
            await self._send_notes(msg, reply.notes, scope.owner, context)
        if state.title and not self.a.store.get(state.path) and group:
            self.a.chats.save(state)        # remember topic names
        asyncio.create_task(self._summarize(state))

    async def _summarize(self, state: ChatState):
        try:
            await self.a.maybe_summarize(state)
        except Exception:
            log.exception("summary failed for %s", state.key)

    async def _typing(self, msg: Message):
        try:
            while True:
                await msg.chat.send_action(ChatAction.TYPING, message_thread_id=msg.message_thread_id)
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _send(self, msg: Message, text: str, **kw):
        for chunk in split_message(md_to_telegram_html(text)):
            try:
                await msg.reply_text(chunk, parse_mode=ParseMode.HTML, do_quote=False, **kw)
            except Exception:
                await msg.reply_text(html.unescape(chunk), do_quote=False, **kw)
            kw = {}

    async def _send_voice(self, msg: Message, text: str, lang: str | None):
        try:
            await msg.chat.send_action(ChatAction.RECORD_VOICE, message_thread_id=msg.message_thread_id)
            with tempfile.TemporaryDirectory() as d:
                out = str(Path(d) / "reply.ogg")
                await audio.synthesize(text, out, lang if lang in audio.VOICES else None)
                with open(out, "rb") as f:
                    await msg.reply_voice(f, do_quote=False)
        except Exception:
            log.exception("voice reply failed")

    async def _send_notes(self, msg: Message, notes, owner_chat: bool, context):
        for n in notes:
            if n.kind == "saved" and owner_chat:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Undo", callback_data=f"undo:{n.ref}")]]) \
                    if n.ref and n.ref != "dry-run" else None
                await msg.reply_text(f"📝 {n.text}", reply_markup=kb, do_quote=False,
                                     disable_notification=True)
            elif n.kind == "pending":
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Save", callback_data=f"ok:{n.ref}"),
                    InlineKeyboardButton("❌ Discard", callback_data=f"no:{n.ref}"),
                ]])
                where = "" if owner_chat else f" (from {msg.chat.title})"
                await context.bot.send_message(self.cfg.owner_id, f"🔐 Confirm memory update{where}:\n{n.text}",
                                               reply_markup=kb)

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not q.from_user or q.from_user.id != self.cfg.owner_id:
            await q.answer("Not allowed.")
            return
        action, _, ref = (q.data or "").partition(":")
        try:
            if action == "undo":
                sha = await self.a.git.revert(ref)
                self.a.store.reload()
                result = f"↩️ Reverted ({sha})"
            elif action == "ok":
                result = await self.a.apply_pending(ref)
            elif action == "no":
                self.a.pending.pop(ref)
                result = "Discarded."
            else:
                result = "Unknown action."
        except Exception as e:
            result = f"Failed: {e}"
        await q.answer()
        await q.edit_message_text(f"{q.message.text}\n\n{result}")

    # ---- commands ----------------------------------------------------------

    async def cmd_help(self, update: Update, context):
        await update.effective_message.reply_text(HELP)

    async def cmd_register(self, update: Update, context):
        msg = update.effective_message
        if msg.chat.type == ChatType.PRIVATE:
            await msg.reply_text("Use /register inside a group.")
            return
        scope = (context.args or ["shared"])[0]
        if scope not in ("owner", "shared"):
            await msg.reply_text("Usage: /register owner|shared")
            return
        g = self.a.chats.load(chat_key(msg.chat.id), title=msg.chat.title or "")
        g.scope = scope
        self.a.chats.save(g)
        await self.a.git.commit(f"chat: register {g.key} ({scope})")
        await msg.reply_text(f"Registered as {scope}. "
                             + ("Only you; full memory." if scope == "owner"
                                else "Shared: I only see files you /share here and reply when mentioned."))

    async def cmd_scope(self, update: Update, context):
        await self.cmd_register(update, context)

    async def _edit_list(self, update: Update, context, field: str, add: bool):
        r = self._resolve(update.effective_message)
        msg = update.effective_message
        if not r:
            await msg.reply_text("This chat is not registered.")
            return
        state, group, _ = r
        target = group if field == "share" and group else state
        items = getattr(target, field)
        args = [a if a.endswith(".md") else a + ".md" for a in (context.args or [])]
        if add:
            missing = [a for a in args if not self.a.store.get(a)]
            if missing or not args:
                await msg.reply_text(f"Unknown file(s): {' '.join(missing) or '(none given)'}")
                return
            items.extend(a for a in args if a not in items)
        else:
            if args:
                items[:] = [i for i in items if i not in args]
            else:
                items.clear()
        self.a.chats.save(target)
        await self.a.git.commit(f"chat: {field} {target.key}")
        await msg.reply_text(f"{field}: {', '.join(items) or '(none)'}")

    async def cmd_bind(self, update, context):
        await self._edit_list(update, context, "bind", True)

    async def cmd_unbind(self, update, context):
        await self._edit_list(update, context, "bind", False)

    async def cmd_share(self, update, context):
        await self._edit_list(update, context, "share", True)

    async def cmd_unshare(self, update, context):
        await self._edit_list(update, context, "share", False)

    async def cmd_chatinfo(self, update: Update, context):
        r = self._resolve(update.effective_message)
        msg = update.effective_message
        if not r:
            await msg.reply_text(f"Not registered. chat id {msg.chat.id}")
            return
        state, group, info = r
        scope = self.a.chats.scope_for(state, group)
        await msg.reply_text(
            f"{info.description}\nkey: {state.key}\nscope: {'owner' if scope.owner else 'shared'}\n"
            f"pinned: {', '.join(state.bind) or '-'}\nshared files: {', '.join(sorted(scope.shared_files)) or '-'}\n"
            f"summary: {len(state.summary)} chars")

    async def _owner_scope(self, update: Update):
        r = self._resolve(update.effective_message)
        if not r:
            return None
        state, group, _ = r
        scope = self.a.chats.scope_for(state, group)
        return scope if scope.owner else None

    async def cmd_memory(self, update: Update, context):
        msg = update.effective_message
        scope = await self._owner_scope(update)
        if not scope:
            await msg.reply_text("Only available in owner chats.")
            return
        if context.args:
            hits = self.a.index.search(" ".join(context.args), scope.can_read, limit=6)
            text = "\n\n".join(f"**{h.path}** › {h.heading}\n{h.text}" for h in hits) or "No matches."
        else:
            text = self.a.store.index(scope)
        await self._send(msg, text)

    async def cmd_show(self, update: Update, context):
        msg = update.effective_message
        scope = await self._owner_scope(update)
        doc = self.a.store.get(context.args[0]) if context.args else None
        if not scope or not doc:
            await msg.reply_text("Usage: /show topics/garden-project.md (owner chats only)")
            return
        await self._send(msg, "```\n" + self.a.store.read_raw(doc.path) + "\n```")

    async def cmd_log(self, update: Update, context):
        out = await self.a.git.log(15)
        await update.effective_message.reply_text(out or "No history.")

    async def cmd_undo(self, update: Update, context):
        msg = update.effective_message
        if not context.args:
            await msg.reply_text("Usage: /undo <sha> (see /log)")
            return
        try:
            sha = await self.a.git.revert(context.args[0])
            self.a.store.reload()
            await msg.reply_text(f"Reverted ({sha}).")
        except Exception as e:
            await msg.reply_text(f"Failed: {e}")

    async def cmd_sync(self, update: Update, context):
        try:
            res = await self.a.git.sync()
            self.a.store.reload()
        except Exception as e:
            res = f"Sync failed: {e}"
        await update.effective_message.reply_text(res)

    async def cmd_voice(self, update: Update, context):
        msg = update.effective_message
        r = self._resolve(msg)
        arg = (context.args or [""])[0]
        if not r or arg not in ("off", "auto", "always"):
            cur = r[0].voice if r else "-"
            await msg.reply_text(f"Voice replies: {cur}\n/voice off | auto | always")
            return
        state = r[0]
        state.voice = arg
        self.a.chats.save(state)
        await self.a.git.commit(f"chat: voice {arg} {state.key}")
        await msg.reply_text(f"Voice replies: {arg}")

    async def cmd_model(self, update: Update, context):
        await update.effective_message.reply_text(
            f"chat: {self.a.chat_model.label}\nmemory: {self.a.memory_model.label}")
