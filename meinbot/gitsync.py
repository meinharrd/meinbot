"""Git operations on the memory repo.

The remote is expected to be a git-remote-gcrypt URL (gcrypt::git@github.com:...),
so GitHub only ever stores encrypted blobs. Locally the repo is plain git.
"""
import asyncio
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class GitError(Exception):
    pass


class GitSync:
    def __init__(self, root: Path, push_delay: int = 30):
        self.root = root
        self.push_delay = push_delay
        self.lock = asyncio.Lock()
        self._push_task: asyncio.Task | None = None
        self.last_error: str | None = None

    def _git(self, *args: str, check: bool = True) -> str:
        r = subprocess.run(["git", "-C", str(self.root), *args],
                           capture_output=True, text=True, timeout=300)
        if check and r.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout.strip()

    async def git(self, *args: str, check: bool = True) -> str:
        return await asyncio.to_thread(self._git, *args, check=check)

    def has_remote(self) -> bool:
        return "origin" in self._git("remote").split()

    async def commit(self, message: str, paths: list[str] | None = None) -> str | None:
        """Stage and commit; returns the short sha, or None if nothing changed."""
        async with self.lock:
            await self.git("add", "-A", "--", *(paths or ["."]))
            status = await self.git("status", "--porcelain", "--", *(paths or ["."]))
            if not status:
                return None
            await self.git("commit", "-q", "-m", message)
            sha = await self.git("rev-parse", "--short", "HEAD")
        self.schedule_push()
        return sha

    async def revert(self, sha: str) -> str:
        async with self.lock:
            await self.git("revert", "--no-edit", sha)
            new = await self.git("rev-parse", "--short", "HEAD")
        self.schedule_push()
        return new

    async def log(self, n: int = 10, path: str | None = None) -> str:
        args = ["log", f"-{n}", "--format=%h %ad %s", "--date=format:%m-%d %H:%M"]
        if path:
            args += ["--", path]
        return await self.git(*args)

    def schedule_push(self):
        """Debounced push: many commits in a burst become one push."""
        if not self.has_remote():
            return
        if self._push_task and not self._push_task.done():
            return
        self._push_task = asyncio.create_task(self._delayed_push())

    async def _delayed_push(self):
        await asyncio.sleep(self.push_delay)
        try:
            await self.sync()
        except Exception as e:  # keep running; surfaced via /sync and last_error
            log.exception("push failed")
            self.last_error = str(e)

    async def sync(self) -> str:
        """Commit stray changes (transcripts), pull --rebase, push."""
        await self.commit("transcripts", ["transcripts"])
        if not self.has_remote():
            return "no remote configured"
        async with self.lock:
            branch = await self.git("rev-parse", "--abbrev-ref", "HEAD")
            remote_branches = await self.git("ls-remote", "--heads", "origin", check=False)
            if f"refs/heads/{branch}" in remote_branches:
                try:
                    await self.git("pull", "-q", "--rebase", "origin", branch)
                except GitError:
                    await self.git("rebase", "--abort", check=False)
                    raise
            await self.git("push", "-q", "origin", branch)
        self.last_error = None
        return "synced"

    async def pull(self) -> bool:
        """Pull only; returns True if HEAD moved."""
        if not self.has_remote():
            return False
        async with self.lock:
            before = await self.git("rev-parse", "HEAD")
            branch = await self.git("rev-parse", "--abbrev-ref", "HEAD")
            remote_branches = await self.git("ls-remote", "--heads", "origin", check=False)
            if f"refs/heads/{branch}" not in remote_branches:
                return False
            await self.git("add", "-A", "transcripts")
            await self.git("commit", "-q", "-m", "transcripts", check=False)
            try:
                await self.git("pull", "-q", "--rebase", "origin", branch)
            except GitError:
                await self.git("rebase", "--abort", check=False)
                raise
            return before != await self.git("rev-parse", "HEAD")
