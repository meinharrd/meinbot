"""Keyword search over memory with SQLite FTS5 (BM25).

The index is in-memory and rebuilt whenever the store reloads, so it never
needs migrating. No model is involved, which keeps retrieval vendor-neutral.
"""
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable

from .memory import Doc, Store

STOPWORDS = set("""
a an and are as at be but by can could did do does for from had has have how i if in into is it its
me my no not of on or our please so than that the their them then there these they this to up was
we were what when where which who why will with would you your about just like also any some
der die das den dem des ein eine einer eines einem und oder aber ist sind war waren bin bist sein
ich du er sie es wir ihr mich mir dich dir uns euch mein meine dein deine nicht kein keine zu im
in an auf aus bei mit nach von vor für über unter wie was wer wo wann warum noch schon auch nur
""".split())


@dataclass
class Hit:
    path: str
    heading: str
    text: str
    score: float


def _chunks(doc: Doc, max_chars: int = 700):
    """Split a document into (heading, text) chunks along headings."""
    heading = doc.title
    buf: list[str] = []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            yield heading, text

    for line in doc.body.split("\n"):
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            yield from flush()
            buf = []
            heading = m.group(1).strip()
            continue
        buf.append(line)
        if sum(len(x) for x in buf) > max_chars:
            yield from flush()
            buf = []
    yield from flush()


class Index:
    def __init__(self, store: Store):
        self.store = store
        self._version = -1
        self.db = sqlite3.connect(":memory:", check_same_thread=False)

    def _rebuild(self):
        db = self.db
        db.execute("DROP TABLE IF EXISTS chunks")
        db.execute(
            "CREATE VIRTUAL TABLE chunks USING fts5(path UNINDEXED, title, aliases, heading, content,"
            " tokenize='unicode61 remove_diacritics 2')"
        )
        rows = []
        for doc in self.store.docs.values():
            if doc.kind == "charter":
                continue
            aliases = " ".join(map(str, doc.meta.get("aliases") or []))
            for heading, text in _chunks(doc):
                rows.append((doc.path, doc.title, aliases, heading, text))
        db.executemany("INSERT INTO chunks VALUES (?,?,?,?,?)", rows)
        self._version = self.store.version

    def search(self, query: str, allow: Callable[[Doc], bool], limit: int = 8,
               exclude: set[str] = frozenset()) -> list[Hit]:
        """Top chunks for a query among documents for which allow(doc) is true."""
        if self._version != self.store.version:
            self._rebuild()
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 3 and t not in STOPWORDS]
        if not terms:
            return []
        q = " OR ".join(f'"{t}"*' for t in dict.fromkeys(terms))
        try:
            rows = self.db.execute(
                "SELECT path, heading, content, bm25(chunks, 0, 6, 6, 2, 1) AS s FROM chunks"
                " WHERE chunks MATCH ? ORDER BY s LIMIT 60", (q,)
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        hits = []
        for path, heading, content, score in rows:
            doc = self.store.docs.get(path)
            if not doc or path in exclude or not allow(doc):
                continue
            hits.append(Hit(path, heading, content, score))
            if len(hits) >= limit:
                break
        return hits
