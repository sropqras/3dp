#!/usr/bin/env bash
# Apply farm/telegram_pack.conf to one printer's moonraker-telegram-bot on enders3dp, keeping [bot] (token/chat)
# and every other section as is. Backs up telegram.conf first; restarts only the bot, never Klipper.
#
# Usage: scripts/telegram_pack.sh <InstanceName> [ssh-host]     e.g. scripts/telegram_pack.sh Ender3MaxNeo
set -euo pipefail
INST="$1"; HOST="${2:-enders3dp}"
PACK="$(cd "$(dirname "$0")/.." && pwd)/farm/telegram_pack.conf"

scp -q -o BatchMode=yes "$PACK" "$HOST:.farm_telegram_pack.conf"
ssh -o BatchMode=yes "$HOST" "python3 - '$INST'" <<'PY'
import configparser, shutil, sys, time
from pathlib import Path
inst = sys.argv[1]
conf = Path.home() / f"{inst}_data/config/telegram.conf"
shutil.copy2(conf, conf.with_name(f"telegram.conf.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
cur = configparser.ConfigParser(interpolation=None); cur.optionxform = str; cur.read(conf)
pack = configparser.ConfigParser(interpolation=None); pack.optionxform = str
pack.read(Path.home() / ".farm_telegram_pack.conf")
for sec in pack.sections():
    if cur.has_section(sec): cur.remove_section(sec)
    cur.add_section(sec)
    for k, v in pack.items(sec): cur.set(sec, k, v)
with open(conf, "w") as f: cur.write(f)
print(f"{conf.name}: replaced {pack.sections()}, kept {[s for s in cur.sections() if s not in pack.sections()]}")
PY

# restart only the bot through Moonraker (the service is whitelisted in moonraker.asvc)
PORT=$(ssh -o BatchMode=yes "$HOST" "grep -E '^\s*port' ~/${INST}_data/config/moonraker.conf | head -1 | tr -dc 0-9")
IP=$(ssh -o BatchMode=yes "$HOST" "hostname -I" | awk '{print $1}')
curl -s -m15 -X POST "http://${IP}:${PORT}/machine/services/restart?service=moonraker-telegram-bot-${INST}"; echo
sleep 6
ssh -o BatchMode=yes "$HOST" "systemctl is-active moonraker-telegram-bot-${INST}; journalctl -u moonraker-telegram-bot-${INST} --since '-1 min' --no-pager -o cat | grep -iE 'error|unknown|invalid|traceback' || echo 'no errors in the bot log since restart'"
