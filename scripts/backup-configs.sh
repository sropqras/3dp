#!/usr/bin/env bash
# Pull Klipper/Moonraker configs for every instance on a farm host into printers/<Name>/.
# Read-only on the printer side. Secrets and auto-generated backups are excluded.
#
# Usage: scripts/backup-configs.sh [ssh-host]   (default: enders3dp, see ~/.ssh/config)
set -euo pipefail

HOST="${1:-enders3dp}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$REPO/printers"

# Files holding tokens/keys, plus noise that doesn't belong in git
EXCLUDES=(
  --exclude='telegram.conf'
  --exclude='moonraker-obico.cfg'
  --exclude='octoeverywhere.conf'
  --exclude='.moonraker.conf.bkp'
  --exclude='moonraker.conf.backup'
  --exclude='printer-2*.cfg'
  --exclude='*.bak-*'
  --exclude='*.conf.20*'
  --exclude='*.zip'
)

instances=$(ssh -o BatchMode=yes "$HOST" 'ls -d ~/*_data 2>/dev/null | xargs -n1 basename')

for inst in $instances; do
  name="${inst%_data}"
  name="${name#printer_}"
  out="$DEST/$name"
  mkdir -p "$out"

  # --no-dereference keeps symlinks (fluidd.cfg, KAMP, timelapse) out of the archive; they are upstream code
  ssh -o BatchMode=yes "$HOST" "cd ~/$inst/config && find . -maxdepth 3 -type f -print0 | tar ${EXCLUDES[*]} --null -T - -cf -" \
    | tar -xf - -C "$out"

  ssh -o BatchMode=yes "$HOST" "cd ~/$inst/config && find . -maxdepth 1 -type l -printf '%f -> %l\n'" \
    > "$out/SYMLINKS.txt"

  echo "$name: $(find "$out" -type f ! -name SYMLINKS.txt | wc -l) files"
done

echo "Backed up from $HOST at $(date '+%Y-%m-%d %H:%M')"
