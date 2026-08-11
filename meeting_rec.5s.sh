#!/bin/zsh
# SwiftBar-плагін: індикатор запису в менюбарі.
# «.5s» у назві = оновлення кожні 5 секунд (конвенція SwiftBar).
# Встановлення: див. README.md (символьне посилання в папку плагінів SwiftBar).

# zsh-модифікатор :A розкриває symlink і працює у штатному macOS без GNU readlink.
DIR="${0:A:h}"
PID_FILE="$DIR/.record.pid"
REQUEST="$DIR/request_record.sh"

recording_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid="$(tr -cd '0-9' < "$PID_FILE")"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    local command="$(ps -p "$pid" -o command= 2>/dev/null)"
    [[ "$command" == *"record.py"* ]] || return 1
    echo "$pid"
}

if PID="$(recording_pid)"; then
    # скільки триває запис (від часу створення pid-файлу)
    START=$(stat -f %m "$PID_FILE")
    ELAPSED=$(( $(date +%s) - START ))
    MIN=$(( ELAPSED / 60 ))
    SEC=$(( ELAPSED % 60 ))
    printf "🔴 %02d:%02d\n" "$MIN" "$SEC"
    echo "---"
    echo "⏹ Зупинити запис | bash='$REQUEST' terminal=false refresh=true"
else
    echo "🎙"
    echo "---"
    echo "🔴 Почати запис | bash='$REQUEST' terminal=false refresh=true"
fi
echo "📂 Відкрити нотатки | bash=/usr/bin/open param1='$DIR/notes' terminal=false"
