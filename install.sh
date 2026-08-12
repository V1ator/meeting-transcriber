#!/bin/zsh
# Meeting Transcriber — встановлення однією командою.
# Ідемпотентний: безпечно запускати повторно, вже зроблені кроки пропускаються.
#
#   chmod +x install.sh && ./install.sh

set -e
umask 077
cd "$(dirname "$0")"

ok()   { echo "  ✅ $1"; }
warn() { echo "  ⚠️  $1"; }
step() { echo "\n▶ $1"; }
MANUAL=()

# ---------- 1. macOS ----------
step "Перевірка macOS"
OS_VER=$(sw_vers -productVersion)
autoload -Uz is-at-least
if ! is-at-least 14.2 "$OS_VER"; then
    echo "❌ Потрібна macOS >= 14.2 (у вас $OS_VER) — catap не працюватиме."; exit 1
fi
ok "macOS $OS_VER"

# ---------- 2. Homebrew ----------
step "Homebrew"
if ! command -v brew >/dev/null; then
    echo "❌ Homebrew не знайдено. Встановіть: https://brew.sh і запустіть скрипт знову."; exit 1
fi
ok "brew є"

# ---------- 3. Базова системна залежність ----------
step "Python 3.12"
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
PY="$(brew --prefix python@3.12)/bin/python3.12"
ok "$($PY --version)"

# ---------- 4. venv + python-залежності ----------
step "Python-оточення"
if [ ! -d .venv ]; then
    "$PY" -m venv .venv
    ok "створено .venv"
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements-base.txt
.venv/bin/pip uninstall -q -y whisperx 2>/dev/null || true
ok "базові залежності встановлено"

# ---------- 5. Ollama ----------
step "Ollama"
if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    if ! command -v ollama >/dev/null; then
        brew install --cask ollama
    fi
    open -a Ollama 2>/dev/null || true
    sleep 3
fi
if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    ok "Ollama працює"
else
    warn "Ollama не відповідає на :11434"
    MANUAL+=("Запустити Ollama.app і додати її в Login Items (щоб піднімалась після ребуту)")
fi

# ---------- 6. .env ----------
step "Конфіг (.env)"
NEW_ENV=false
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    NEW_ENV=true
else
    ok ".env вже існує — не чіпаю"
fi
chmod 600 .env

if [ "$NEW_ENV" = true ]; then
    echo ""
    .venv/bin/python3 modules.py configure --no-apply
    if grep -Eq '^AUDIO_PIPELINE_ENABLED=(true|1|yes|on)([[:space:]]|$)' .env; then
        echo ""
        echo "  Для аудіомодуля введіть HuggingFace токен (hf_...), Enter — пропустити:"
        read -rs HF_INPUT
        echo ""
        if [ -n "$HF_INPUT" ]; then
            ENV_TMP="$(mktemp ./.env.XXXXXX)"
            trap 'rm -f "$ENV_TMP"' EXIT HUP INT TERM
            while IFS= read -r line; do
                if [[ "$line" == HF_TOKEN=* ]]; then
                    printf 'HF_TOKEN=%s\n' "$HF_INPUT" >> "$ENV_TMP"
                else
                    printf '%s\n' "$line" >> "$ENV_TMP"
                fi
            done < .env
            chmod 600 "$ENV_TMP"
            mv "$ENV_TMP" .env
            trap - EXIT HUP INT TERM
            unset HF_INPUT
            ok "HF_TOKEN записано в .env"
        else
            MANUAL+=("Вписати HF_TOKEN у .env (токен: https://huggingface.co/settings/tokens)")
        fi
    fi
fi

# ---------- 7. Опційний аудіомодуль ----------
if grep -Eq '^AUDIO_PIPELINE_ENABLED=(true|1|yes|on)([[:space:]]|$)' .env; then
    step "Аудіозалежності"
    brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
    .venv/bin/pip install -q -r requirements-audio.txt
    ok "ffmpeg, Whisper і діаризація встановлені"
else
    step "Аудіозалежності"
    ok "пропущено — обрано Meet-only режим"
fi

# ---------- 8. Модель ----------
step "LLM-модель"
MODEL=$(grep '^OLLAMA_MODEL=' .env | cut -d= -f2)
if curl -sf http://localhost:11434/api/tags 2>/dev/null | grep -Fq "\"$MODEL\""; then
    ok "$MODEL вже завантажена"
elif command -v ollama >/dev/null && curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "  Завантажую $MODEL (~17 GB, довго)..."
    ollama pull "$MODEL" && ok "$MODEL готова"
else
    MANUAL+=("ollama pull $MODEL")
fi

# ---------- 9. Права на скрипти ----------
chmod +x modules.py toggle_record.sh request_record.sh meeting_rec.5s.sh
mkdir -p recordings transcripts notes failed logs
chmod 700 recordings transcripts notes failed logs

# ---------- 10. LaunchAgents (watcher + mic-autostart) ----------
# plist-и генеруються з поточного шляху проекту — папку можна переносити,
# достатньо перезапустити install.sh.
step "Фонові сервіси (launchd)"
PROJ="$(pwd)"
SERVICE_LOG_DIR="$HOME/Library/Logs/MeetingTranscriber"
mkdir -p "$HOME/Library/LaunchAgents" "$SERVICE_LOG_DIR"
chmod 700 "$HOME/Library/LaunchAgents" "$SERVICE_LOG_DIR"
touch "$SERVICE_LOG_DIR/watcher.log" "$SERVICE_LOG_DIR/mic-autostart.log"
chmod 600 "$SERVICE_LOG_DIR/watcher.log" "$SERVICE_LOG_DIR/mic-autostart.log"

make_plist() {  # $1 = label, далі — ProgramArguments
    local LABEL=$1; shift
    local ARGS=""
    for a in "$@"; do ARGS+="        <string>$a</string>\n"; done
    cat > ~/Library/LaunchAgents/$LABEL.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
$(printf '%b' "$ARGS")    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$SERVICE_LOG_DIR/${LABEL##*.}.log</string>
    <key>StandardErrorPath</key><string>$SERVICE_LOG_DIR/${LABEL##*.}.log</string>
    <key>ProcessType</key><string>Background</string>
    <key>ThrottleInterval</key><integer>30</integer>
</dict>
</plist>
PLIST
}

make_plist local.meeting-transcriber.watcher "$PROJ/.venv/bin/python3" "$PROJ/watch_and_process.py"
make_plist local.meeting-transcriber.mic-autostart "$PROJ/.venv/bin/python3" "$PROJ/mic_watch.py"

.venv/bin/python3 modules.py apply
echo ""
.venv/bin/python3 modules.py status

# ---------- Підсумок ----------
echo "\n════════════════════════════════════════"
echo "Встановлення завершено."
if grep -Eq '^AUDIO_PIPELINE_ENABLED=(true|1|yes|on)([[:space:]]|$)' .env; then
    MANUAL+=("Прийняти умови моделей pyannote на HuggingFace (segmentation-3.0, speaker-diarization-community-1)")
    MANUAL+=("Повісити request_record.sh на хоткей у Raycast/Shortcuts — див. README.md")
    MANUAL+=("Перший запуск запису: дати дозволи мікрофона і System Audio Recording (TCC)")
    MANUAL+=("Записувати в НАВУШНИКАХ і за згодою учасників")
fi
echo "Лишилось руками:"
for item in "${MANUAL[@]}"; do echo "  • $item"; done
