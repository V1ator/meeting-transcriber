#!/bin/zsh
# Meeting Transcriber — встановлення однією командою.
# Ідемпотентний: безпечно запускати повторно, вже зроблені кроки пропускаються.
#
#   chmod +x install.sh && ./install.sh

set -e
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

# ---------- 3. Системні залежності ----------
step "ffmpeg і Python 3.12"
brew list ffmpeg >/dev/null 2>&1     || brew install ffmpeg
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
PY="$(brew --prefix python@3.12)/bin/python3.12"
ok "ffmpeg + $($PY --version)"

# ---------- 4. venv + python-залежності ----------
step "Python-оточення"
if [ ! -d .venv ]; then
    "$PY" -m venv .venv
    ok "створено .venv"
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip uninstall -q -y whisperx 2>/dev/null || true
ok "залежності встановлено"

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
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "  Введіть HuggingFace токен (hf_...), Enter — пропустити:"
    read -r HF_INPUT
    if [ -n "$HF_INPUT" ]; then
        ENV_TMP=".env.$$.tmp"
        : > "$ENV_TMP"
        while IFS= read -r line; do
            if [[ "$line" == HF_TOKEN=* ]]; then
                printf 'HF_TOKEN=%s\n' "$HF_INPUT" >> "$ENV_TMP"
            else
                printf '%s\n' "$line" >> "$ENV_TMP"
            fi
        done < .env
        chmod 600 "$ENV_TMP"
        mv "$ENV_TMP" .env
        unset HF_INPUT
        ok "HF_TOKEN записано в .env"
    else
        MANUAL+=("Вписати HF_TOKEN у .env (токен: https://huggingface.co/settings/tokens)")
    fi
else
    ok ".env вже існує — не чіпаю"
fi
chmod 600 .env

# ---------- 7. Модель ----------
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

# ---------- 8. Права на скрипти ----------
chmod +x toggle_record.sh request_record.sh meeting_rec.5s.sh
mkdir -p recordings transcripts notes failed logs

# ---------- 9. LaunchAgents (watcher + mic-autostart) ----------
# plist-и генеруються з поточного шляху проекту — папку можна переносити,
# достатньо перезапустити install.sh.
step "Фонові сервіси (launchd)"
PROJ="$(pwd)"

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
    <key>StandardOutPath</key><string>$PROJ/logs/${LABEL##*.}.log</string>
    <key>StandardErrorPath</key><string>$PROJ/logs/${LABEL##*.}.log</string>
    <key>ProcessType</key><string>Background</string>
    <key>ThrottleInterval</key><integer>30</integer>
</dict>
</plist>
PLIST
}

make_plist local.meeting-transcriber.watcher "$PROJ/.venv/bin/python3" "$PROJ/watch_and_process.py"
make_plist local.meeting-transcriber.mic-autostart "$PROJ/.venv/bin/python3" "$PROJ/mic_watch.py"

for AGENT in local.meeting-transcriber.watcher local.meeting-transcriber.mic-autostart; do
    launchctl bootout "gui/$(id -u)/$AGENT" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/$AGENT.plist
done
sleep 2
launchctl list | grep -q meeting-transcriber && ok "watcher запущено (logs/watcher.log)" \
    || warn "watcher не піднявся — logs/watcher.log"
launchctl list | grep -q mic-autostart && ok "mic-autostart запущено (logs/mic-autostart.log)" \
    || warn "mic-autostart не піднявся — logs/mic-autostart.log"

# ---------- Підсумок ----------
echo "\n════════════════════════════════════════"
echo "Встановлення завершено."
MANUAL+=("Прийняти умови моделей pyannote на HuggingFace (segmentation-3.0, speaker-diarization-community-1)")
MANUAL+=("Повісити request_record.sh на хоткей у Raycast/Shortcuts — див. README.md")
MANUAL+=("Перший запуск запису: дати дозволи мікрофона і System Audio Recording (TCC)")
MANUAL+=("Записувати в НАВУШНИКАХ і за згодою учасників")
echo "Лишилось руками:"
for item in "${MANUAL[@]}"; do echo "  • $item"; done
