---
title: Memory repo conventions
description: How this memory is organised; read by humans and by any model
sensitivity: public
kind: meta
---
# Memory repo conventions

This repository is the long-term memory of a personal assistant.
It is plain Markdown so that any model, from any vendor, can use it.
The bot code lives elsewhere and is replaceable; this repo is the asset.

## Layout

- `charter.md`: who the assistant is and how it behaves (injected first, always)
- `core.md`: the most important facts about the owner (always injected in owner chats)
- `topics/`: one file per area of life or work
- `people/`: one file per person
- `vault/`: secret material (identifiers, addresses, date of birth); never auto-injected
- `chats/`: per-chat settings and rolling conversation summaries
- `journal/`: dated episodic notes
- `transcripts/`: raw conversation logs (JSONL, one file per chat per month)
- `sources/`: original imports, never edited

## Frontmatter

Every file starts with YAML frontmatter:

- `title`, `description`: used for the auto-generated index
- `sensitivity`: `public` < `personal` < `private` < `secret`
- `aliases`: extra search terms (optional)
- `updated`: last change date

## Fact notation

One fact per bullet, followed by its source tag and date:

    - Moved to Lisbon in 2024. [U] (as of 2026-10)

Source tags:

- `P` profile/preferences the owner set in a previous assistant
- `M` memory imported from a previous assistant
- `C` excerpt of a previous assistant's recent chats
- `R` retrieved history from a previous assistant (owner statement or pasted document)
- `A` assistant-authored (drafts, analyses); not the owner's own statement
- `I` inference; not stated by the owner
- `U` the owner told this assistant directly
- `D` from a document the owner shared with this assistant

Facts are never deleted. A fact that is no longer true is struck through
and annotated: `- ~~old fact~~ [M] (superseded 2026-11-02: reason)`,
with the replacement fact on the next line.
