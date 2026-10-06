"""The memory store: Markdown files with YAML frontmatter in a git repo.

Everything the assistant knows lives here as plain text. Search indexes are
derived from these files and can always be rebuilt.
"""
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LEVELS = ["public", "personal", "private", "secret"]
# Never indexed or searched: raw imports and conversation logs.
EXCLUDED_DIRS = {"sources", "transcripts", ".git"}
# Directories the model may create files in.
CREATABLE_DIRS = ("topics/", "people/", "journal/", "vault/")
# Files the model must never edit.
READ_ONLY = {"charter.md", "README.md"}

FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


def level_of(name: str) -> int:
    return LEVELS.index(name) if name in LEVELS else LEVELS.index("secret")


@dataclass
class Doc:
    path: str          # relative to the memory root, e.g. "topics/garden-project.md"
    meta: dict
    body: str

    @property
    def level(self) -> int:
        # Files without a valid sensitivity are treated as secret: fail closed.
        return level_of(self.meta.get("sensitivity", "secret"))

    @property
    def title(self) -> str:
        return self.meta.get("title") or self.path

    @property
    def kind(self) -> str:
        return self.meta.get("kind", "")


@dataclass
class Scope:
    """What a chat may see. Enforced in code, never left to the model."""
    owner: bool                       # private to the owner (DM or owner group)
    shared_files: set[str] = field(default_factory=set)

    def can_inject(self, doc: Doc) -> bool:
        """May this file be put into context automatically?"""
        if self.owner:
            return doc.level <= level_of("private")
        return doc.level == 0 or doc.path in self.shared_files

    def can_read(self, doc: Doc) -> bool:
        """May the model read this file on explicit request?"""
        if self.owner:
            return True
        return self.can_inject(doc)


def parse(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, text[m.end():]


def today() -> str:
    return dt.date.today().isoformat()


def month() -> str:
    return dt.date.today().strftime("%Y-%m")


class MemoryError_(Exception):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.docs: dict[str, Doc] = {}
        self.version = 0          # bumped on every reload, lets indexes rebuild lazily
        self.reload()

    # ---- loading -------------------------------------------------------

    def reload(self):
        docs = {}
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root).as_posix()
            if rel.split("/", 1)[0] in EXCLUDED_DIRS:
                continue
            meta, body = parse(p.read_text(encoding="utf-8"))
            docs[rel] = Doc(rel, meta, body)
        self.docs = docs
        self.version += 1

    def get(self, path: str) -> Doc | None:
        return self.docs.get(self._norm(path))

    @property
    def charter(self) -> str:
        d = self.docs.get("charter.md")
        return d.body.strip() if d else ""

    @property
    def core(self) -> Doc | None:
        return self.docs.get("core.md")

    def index(self, scope: Scope) -> str:
        """One line per visible file, generated from frontmatter."""
        lines = []
        for d in self.docs.values():
            if d.kind in ("charter", "core", "meta") or d.path.startswith(("chats/", "journal/")):
                continue
            if not scope.can_read(d):
                continue
            tag = f" [{LEVELS[d.level]}]" if scope.owner else ""
            desc = d.meta.get("description", "")
            lines.append(f"- {d.path}: {d.title}. {desc}{tag}")
        return "\n".join(lines)

    # ---- writing -------------------------------------------------------

    def _norm(self, path: str) -> str:
        path = path.strip().lstrip("/")
        if not path.endswith(".md"):
            path += ".md"
        return path

    def _check_path(self, path: str) -> str:
        path = self._norm(path)
        if ".." in path.split("/") or path.split("/", 1)[0] in EXCLUDED_DIRS:
            raise MemoryError_(f"invalid path: {path}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9/_.-]*\.md", path):
            raise MemoryError_(f"path must be lowercase kebab-case: {path}")
        return path

    def write_raw(self, path: str, text: str):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        self.reload()

    def read_raw(self, path: str) -> str:
        return (self.root / self._norm(path)).read_text(encoding="utf-8")

    def target_level(self, update: dict) -> int:
        """Sensitivity of the file an update would touch."""
        path = self._norm(update.get("path", ""))
        doc = self.docs.get(path)
        if doc:
            return doc.level
        return level_of(update.get("sensitivity", "private"))

    def apply(self, update: dict) -> str:
        """Apply a structured update. Returns a one-line description.

        update = {op: add|supersede|create, path, text, section?, old_text?,
                  reason?, source?, as_of?, title?, description?, sensitivity?}
        """
        op = update.get("op")
        path = self._check_path(update.get("path", ""))
        if path in READ_ONLY:
            raise MemoryError_(f"{path} can only be edited by hand")
        text = (update.get("text") or "").strip()
        if text.startswith("- "):
            text = text[2:]
        source = (update.get("source") or "U").strip("[] ")
        as_of = update.get("as_of") or month()
        bullet = f"- {text} [{source}] (as of {as_of})"

        if op == "create":
            if path in self.docs:
                raise MemoryError_(f"{path} already exists; use add")
            if not path.startswith(CREATABLE_DIRS):
                raise MemoryError_(f"new files go in {', '.join(CREATABLE_DIRS)}")
            sens = update.get("sensitivity", "private")
            if sens not in LEVELS:
                raise MemoryError_(f"sensitivity must be one of {LEVELS}")
            meta = {
                "title": update.get("title") or path.rsplit("/", 1)[-1][:-3].replace("-", " ").title(),
                "description": update.get("description", ""),
                "sensitivity": sens,
                "updated": today(),
            }
            fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
            body = f"# {meta['title']}\n\n{bullet}\n" if text else f"# {meta['title']}\n"
            self.write_raw(path, f"---\n{fm}\n---\n{body}")
            return f"created {path}"

        if path not in self.docs:
            raise MemoryError_(f"{path} does not exist; use op=create")
        raw = self.read_raw(path)
        if not text:
            raise MemoryError_("text is required")

        if op == "add":
            raw = _insert_bullet(raw, bullet, update.get("section"))
            desc = f"added to {path}: {text}"
        elif op == "supersede":
            old = (update.get("old_text") or "").strip()
            if not old:
                raise MemoryError_("old_text is required for supersede")
            lines = raw.split("\n")
            hits = [i for i, l in enumerate(lines)
                    if l.lstrip().startswith("- ") and old.lower() in l.lower() and "~~" not in l]
            if not hits:
                raise MemoryError_(f"no current fact in {path} contains: {old!r}")
            if len(hits) > 1:
                raise MemoryError_(f"old_text matches {len(hits)} facts; be more specific")
            i = hits[0]
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            content = lines[i].lstrip()[2:]
            reason = update.get("reason", "").strip()
            note = f"superseded {today()}" + (f": {reason}" if reason else "")
            lines[i] = f"{indent}- ~~{content}~~ ({note})"
            lines.insert(i + 1, indent + bullet)
            raw = "\n".join(lines)
            desc = f"updated {path}: {text}"
        else:
            raise MemoryError_(f"unknown op: {op}")

        raw = _touch_updated(raw)
        self.write_raw(path, raw)
        return desc


def _touch_updated(raw: str) -> str:
    m = FM_RE.match(raw)
    if not m:
        return raw
    fm = m.group(1)
    if re.search(r"^updated:.*$", fm, re.M):
        fm = re.sub(r"^updated:.*$", f"updated: {today()}", fm, flags=re.M)
    else:
        fm += f"\nupdated: {today()}"
    return f"---\n{fm}\n---\n" + raw[m.end():]


def _insert_bullet(raw: str, bullet: str, section: str | None) -> str:
    """Append a bullet at the end of a section (created if missing)."""
    lines = raw.rstrip("\n").split("\n")
    if section:
        sec = section.strip().lstrip("#").strip()
        start = next((i for i, l in enumerate(lines)
                      if re.match(r"^#{2,6}\s+", l) and l.lstrip("#").strip().lower() == sec.lower()), None)
        if start is None:
            return "\n".join(lines) + f"\n\n## {sec}\n{bullet}\n"
        level = len(lines[start]) - len(lines[start].lstrip("#"))
        end = len(lines)
        for j in range(start + 1, len(lines)):
            m = re.match(r"^(#{1,6})\s+", lines[j])
            if m and len(m.group(1)) <= level:
                end = j
                break
        # insert after the last non-blank line of the section
        k = end
        while k > start + 1 and not lines[k - 1].strip():
            k -= 1
        lines.insert(k, bullet)
        return "\n".join(lines) + "\n"
    return "\n".join(lines) + f"\n{bullet}\n"
