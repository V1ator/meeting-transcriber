#!/bin/zsh
# SwiftBar-плагін: індикатор запису в менюбарі.
# «.5s» у назві = оновлення кожні 5 секунд (конвенція SwiftBar).
# Встановлення: див. README.md (символьне посилання в папку плагінів SwiftBar).

# Шлях до проекту визначається через symlink — працює після перенесення папки
DIR="$(dirname "$(readlink -f "$0")")"
PID_FILE="$DIR/.record.pid"
REQUEST="$DIR/request_record.sh"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
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
