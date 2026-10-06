#!/usr/bin/env bash
# Initialise the memory repo, create its encryption key, and (optionally)
# connect an encrypted GitHub remote via git-remote-gcrypt.
#
#   scripts/setup_memory.sh                         # local repo + key only
#   scripts/setup_memory.sh git@github.com:user/repo.git   # also add + push encrypted remote
set -euo pipefail

MEM="${MEINBOT_MEMORY:-$HOME/meinbot-memory}"
STATE="$(cd "$(dirname "$0")/.." && pwd)/state"
UID_STR="meinbot memory <meinbot-memory@localhost>"
mkdir -p "$STATE"; chmod 700 "$STATE"

command -v git-remote-gcrypt >/dev/null || { echo "install git-remote-gcrypt first"; exit 1; }

# 1. Encryption key: one dedicated key, no passphrase so the bot can run unattended.
#    Protect it like the data itself: the exported backup goes in your password manager.
if ! gpg --list-secret-keys "$UID_STR" >/dev/null 2>&1; then
  gpg --batch --pinentry-mode loopback --passphrase '' \
      --quick-generate-key "$UID_STR" ed25519 sign,cert never
  FPR=$(gpg --list-keys --with-colons "$UID_STR" | awk -F: '/^fpr/{print $10; exit}')
  gpg --batch --pinentry-mode loopback --passphrase '' --quick-add-key "$FPR" cv25519 encr never
fi
FPR=$(gpg --list-keys --with-colons "$UID_STR" | awk -F: '/^fpr/{print $10; exit}')
if [ ! -f "$STATE/memory-key-BACKUP.asc" ]; then
  gpg --armor --export-secret-keys "$FPR" > "$STATE/memory-key-BACKUP.asc"
  chmod 600 "$STATE/memory-key-BACKUP.asc"
fi
echo "key: $FPR"

# 2. Local repo (seeded from the example memory on first run)
if [ ! -d "$MEM" ]; then
  cp -r "$(cd "$(dirname "$0")/.." && pwd)/examples/memory" "$MEM"
  echo "created $MEM from examples/memory; edit charter.md and core.md to make it yours"
fi
cd "$MEM"
if [ ! -d .git ]; then
  git init -q -b main
  git config user.name "meinbot"
  git config user.email "meinbot-memory@localhost"
  git add -A && git commit -qm "initial memory import"
fi

# 3. Encrypted remote
if [ $# -ge 1 ]; then
  git remote remove origin 2>/dev/null || true
  git remote add origin "gcrypt::$1"
  git config remote.origin.gcrypt-participants "$FPR"
  git config remote.origin.gcrypt-signingkey "$FPR"
  git config remote.origin.gcrypt-publish-participants false
  git push -u origin main
  echo "pushed encrypted memory to $1"
fi
