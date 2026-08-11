#!/bin/zsh
# Передає ручний toggle постійному mic-autostart LaunchAgent.
# Цей процес ніколи не відкриває мікрофон, тому SwiftBar не потребує TCC-дозволу.

set -eu
cd "$(dirname "$0")"

audio_module_enabled() {
    local value="true"
    if [ -f .env ]; then
        value="$(sed -n 's/^[[:space:]]*AUDIO_PIPELINE_ENABLED[[:space:]]*=[[:space:]]*//p' .env | tail -n 1)"
        value="${value%%#*}"
        value="$(printf '%s' "$value" | tr -d "[:space:]'\"")"
    fi
    case "${value:l}" in false|0|no|off) return 1;; esac
    return 0
}

if ! audio_module_enabled; then
    osascript -e 'display notification "Модуль запису звуку вимкнено" with title "Meeting Transcriber"' 2>/dev/null || true
    echo "Модуль audio вимкнено" >&2
    exit 3
fi

REQUEST_DIR=".control/requests"
mkdir -p "$REQUEST_DIR"
chmod 700 .control "$REQUEST_DIR" 2>/dev/null || true

STAMP="$(date +%s).$$"
TMP="$REQUEST_DIR/.$STAMP.tmp"
REQUEST="$REQUEST_DIR/$STAMP.request"

(umask 077; printf 'toggle\n' > "$TMP")
mv "$TMP" "$REQUEST"
