# meinbot

A personal assistant on Telegram whose memory belongs to you, not to a model vendor.

- **Memory is plain Markdown** in a git repo: one file per topic, person or
  chat, each fact tagged with its source and date. Any model can read it, and
  so can you.
- **Synced and backed up to GitHub encrypted** with
  [git-remote-gcrypt](https://spwhitton.name/tech/code/git-remote-gcrypt/).
  GitHub sees opaque blobs; every device with the key gets a normal git repo.
- **Vendor-independent.** A small context compiler builds the same plain-text
  context for every model. Adapters exist for the Anthropic API, any
  OpenAI-compatible API (OpenAI, OpenRouter, Gemini, Ollama, vLLM, …) and
  Claude Code subscriptions. Switching is a config change. An eval set tells you
  whether the new model still answers from memory correctly.
- **Privacy enforced in code.** Every file has a sensitivity level. In chats
  with other people the model never *receives* private memory, so it cannot
  leak it.
- **Voice.** Voice notes are transcribed locally (faster-whisper) and answered
  with local TTS (piper).
- **Web access.** `web_search` (DuckDuckGo, no key) and `web_fetch` (readable
  text via trafilatura, with headless Chromium for JavaScript pages) run
  locally, so every vendor gets them. Requests to private or loopback
  addresses are refused at every redirect and for every request a rendered
  page makes, and page content is marked as untrusted.

About 2,000 lines of Python. It is meant to be read and adapted.

## How it works

```
Telegram ──► telegram_bot.py ──► agent.py ──► providers.py ──► any model
                 │                  │  ▲
                 │            compiler.py   tools.py (memory_search/_read/_update, web_search/_fetch)
                 │                  │          │
                 └── transcripts ───┴──► memory repo (Markdown + git) ──► GitHub (encrypted)
```

### The memory repo

```
charter.md      who the assistant is and how it behaves (always in context)
core.md         the most important facts about you (always in context)
topics/*.md     one file per area of life or work
people/*.md     one file per person
vault/*.md      secret material, never injected automatically
chats/*.md      per-chat settings + rolling conversation summary
journal/        dated notes
transcripts/    raw conversation logs (JSONL), so memory can be re-extracted later
evals.yaml      questions to check a model against this memory
```

A file looks like this:

```markdown
---
title: Community garden
description: Neighbourhood garden project; plot, budget, people involved
sensitivity: personal          # public < personal < private < secret
aliases: [garden, plot]
---
# Community garden

- Budget for year one is €3,200. [U] (as of 2026-09)
- ~~Planting starts Nov 2026.~~ [U] (superseded 2026-10-02: delayed)
- Planting starts March 2027. [U] (as of 2026-10)
```

Facts are never deleted, only superseded. The source tag records who said it
(`U` you, `D` a document, `A` an assistant draft, `I` an inference), because
the worst failure of long-term memory is a plan or a draft quietly turning
into "fact".

See `examples/memory/` for a complete fictional memory.

### Context compiler (`compiler.py`)

Every turn, the model gets:

1. **system:** charter · core profile · index of all visible files · files
   pinned to this chat · chat description · rolling summary of the chat
2. **messages:** recent turns, then the new message, preceded by search
   excerpts (SQLite FTS5/BM25, no embeddings needed) and the current time.

Stable parts come first, so vendors with prompt caching can reuse them. The
model can fetch more with `memory_search` and `memory_read`, and writes with
`memory_update` (`add`, `supersede` or `create`). Every write is a git commit
with an Undo button in Telegram.

### Scopes

| Chat | Auto-injected | Readable on request | Writes |
|---|---|---|---|
| DM / owner group | up to `private` | everything, incl. vault | applied (secret: confirm) |
| Shared group | `public` + files `/share`d there | same | always need your confirmation |

### Providers (`providers.py`)

All adapters speak one neutral message format. Adding a vendor is one class.

```yaml
models:
  chat:
    provider: openai
    base_url: http://localhost:11434/v1   # Ollama, fully local
    model: qwen3.6:27b
```

The `claude_code` provider runs Claude through the Claude Agent SDK on a
logged-in Claude Code installation. Built-in tools and settings are switched
off, so the model sees only this bot's prompt and memory tools.

## Setup

Requirements: Python 3.11+, git, gpg, `git-remote-gcrypt`, ffmpeg (voice).

```bash
git clone https://github.com/meinharrd/meinbot && cd meinbot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml      # owner name, models
cp .env.example .env                    # Telegram token, your Telegram user id, API key
scripts/setup_memory.sh                 # creates the key + ~/meinbot-memory from the example
# edit ~/meinbot-memory/charter.md and core.md, add your own topics
scripts/setup_memory.sh git@github.com:you/your-private-memory.git   # encrypted remote
.venv/bin/python -m meinbot ask "what do you know about me?"         # terminal test
.venv/bin/python -m meinbot.eval                                     # memory eval
.venv/bin/python -m meinbot                                          # run the bot
```

Headless browser for JavaScript pages:

```bash
.venv/bin/python -m playwright install chromium-headless-shell
```

Voices for replies (English and German are configured in `audio.py`):

```bash
.venv/bin/python -m piper.download_voices en_US-lessac-medium --data-dir state/voices
```

**Back up `state/memory-key-BACKUP.asc`** (e.g. in your password manager).
Without it the GitHub copy cannot be decrypted. On another machine:
`gpg --import memory-key-BACKUP.asc && git clone -b main gcrypt::git@github.com:you/your-private-memory.git`.

### Telegram

- Create a bot with @BotFather. The bot only talks to the user id in `.env`.
- For topic groups, disable privacy mode (`/setprivacy`) so the bot sees all
  messages. Then send `/register owner` in the group and `/bind topics/x.md`
  in each topic.
- For groups with other people: `/register shared` and `/share` the files it
  may use. There it replies only when mentioned.
- `/help` lists all commands.

## Trust boundaries

- GitHub holds only ciphertext.
- The server running the bot holds the plaintext working copy and the key.
- The active model vendor sees whatever is injected for a turn. Scopes keep
  that to the minimum, and a local model removes the vendor entirely.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## License

MIT
