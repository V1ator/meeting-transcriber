#!/bin/zsh
# Передає ручний toggle постійному mic-autostart LaunchAgent.
# Цей процес ніколи не відкриває мікрофон, тому SwiftBar не потребує TCC-дозволу.

set -eu
cd "$(dirname "$0")"

REQUEST_DIR=".control/requests"
mkdir -p "$REQUEST_DIR"
chmod 700 .control "$REQUEST_DIR" 2>/dev/null || true

STAMP="$(date +%s).$$"
TMP="$REQUEST_DIR/.$STAMP.tmp"
REQUEST="$REQUEST_DIR/$STAMP.request"

(umask 077; printf 'toggle\n' > "$TMP")
mv "$TMP" "$REQUEST"
