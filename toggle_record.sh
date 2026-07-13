#!/bin/zsh
# Тумблер запису: перший виклик — старт record.py у фоні, другий — коректний стоп (SIGINT).
# Повісьте на хоткей у Raycast (Script Command) або Shortcuts.
#
# ⚠️ TCC: дозволи мікрофона і System Audio Recording прив'язуються до застосунку,
# з якого стартував процес (Raycast/Shortcuts) — при першому запуску macOS
# спитає ще раз, це нормально.

cd "$(dirname "$0")"
PID_FILE=".record.pid"
LOCK_DIR=".record.toggle.lock"
MODE="${1:-}"
DIALOG_TIMEOUT=25
mkdir -p logs
# .env: інші налаштування діють і при запуску з хоткея; режим задає popup/аргумент
set -a; [ -f .env ] && source .env; set +a

notify() {
    osascript -e "display notification \"$1\" with title \"Meeting Transcriber\""
}

if [ "$#" -gt 1 ] || { [ -n "$MODE" ] && [ "$MODE" != "--aec" ] && [ "$MODE" != "--raw" ]; }; then
    notify "Невідомий режим запису"
    exit 2
fi

choose_mode() {
    local result
    result="$(osascript -e "display dialog \"Як записувати мікрофон?\\n\\nНавушники — один локальний спікер.\\nДинаміки — кілька людей біля мікрофона.\\n\\nПереконайтеся, що учасники в курсі.\" with title \"Meeting Transcriber\" buttons {\"Пропустити\", \"Навушники\", \"Динаміки\"} default button \"Динаміки\" giving up after $DIALOG_TIMEOUT")"
    [ -n "$result" ] || return 1
    [[ "$result" == *"gave up:true"* ]] && return 1
    [[ "$result" == *"button returned:Навушники"* ]] && { echo "--raw"; return 0; }
    [[ "$result" == *"button returned:Динаміки"* ]] && { echo "--aec"; return 0; }
    return 1
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    notify "Зачекайте: попередня команда запису ще виконується"
    exit 1
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT

recording_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid="$(tr -cd '0-9' < "$PID_FILE")"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    local command="$(ps -p "$pid" -o command= 2>/dev/null)"
    [ -z "$command" ] || [[ "$command" == *"record.py"* ]] || return 1
    echo "$pid"
}

if PID="$(recording_pid)"; then
    kill -INT "$PID"
    # record.py сам прибере PID лише після flush, mono-конвертації й manifest.
    for _ in {1..60}; do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$PID" 2>/dev/null; then
        notify "⏳ Запис зупиняється й фіналізує аудіо"
    else
        rm -f "$PID_FILE"
        notify "⏹ Запис безпечно завершено — файл піде в обробку"
    fi
else
    rm -f "$PID_FILE"
    if [ -z "$MODE" ]; then
        MODE="$(choose_mode)" || exit 0
    fi
    nohup .venv/bin/python3 record.py "$MODE" >> logs/record.log 2>&1 &
    PID=$!
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
        (umask 077; printf '%s\n' "$PID" > "$PID_FILE")
        if [ "$MODE" = "--aec" ]; then
            notify "🔴 Йде запис — режим Динаміки (AEC)"
        else
            notify "🔴 Йде запис — режим Навушники (Raw)"
        fi
    else
        if tail -n 12 logs/record.log | grep -q "MICROPHONE_PERMISSION_DENIED"; then
            notify "Немає доступу до мікрофона — дозвольте launcher у Privacy & Security → Microphone"
        elif tail -n 12 logs/record.log | grep -q "MICROPHONE_DEVICE_MISSING"; then
            notify "macOS не бачить пристрій мікрофона — перевірте Sound → Input"
        else
            notify "Не вдалося почати запис — див. logs/record.log"
        fi
        exit 1
    fi
fi
