"""Markdown to Telegram HTML and message splitting (adapted from github.com/meinharrd/sir-vibe-a-lot)."""
import html
import re

TG_LIMIT = 4000  # official limit is 4096; leave headroom for tags


def md_to_telegram_html(text: str) -> str:
    """Best-effort markdown -> Telegram HTML. Telegram supports only a small
    tag set (b, i, s, u, code, pre, a, blockquote)."""
    out = []
    # Handle fenced code blocks separately so we don't mangle their contents.
    parts = re.split(r"(```[\s\S]*?```)", text)
    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            body = part[3:-3]
            # drop optional language line
            if "\n" in body:
                first, rest = body.split("\n", 1)
                if first.strip() and " " not in first.strip():
                    body = rest
            out.append(f"<pre>{html.escape(body.strip('\n'))}</pre>")
            continue
        t = html.escape(part)
        t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*([^*\n][^*]*?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<i>\1</i>", t)
        t = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", t)
        t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.MULTILINE)
        # Markdown links become "text: url" with the bare URL visible. A
        # hidden-text anchor from a bot makes every Telegram client show an
        # "Open this link?" prompt; a plain URL opens directly. If the text
        # already is the URL, print it once.
        t = re.sub(
            r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
            lambda m: m.group(2) if m.group(1).strip() == m.group(2) else f"{m.group(1)}: {m.group(2)}",
            t,
        )
        t = re.sub(r"^(\s*)[-*]\s+", r"\1• ", t, flags=re.MULTILINE)
        out.append(t)
    return "".join(out).strip()


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks, preferring paragraph breaks and
    never splitting inside a <pre> block if avoidable."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while len(text) > limit:
        window = text[:limit]
        # don't cut a <pre> block in half
        if window.count("<pre>") > window.count("</pre>"):
            cut = window.rfind("<pre>")
        else:
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return [c for c in chunks if c]
