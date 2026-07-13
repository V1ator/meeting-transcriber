# Meeting Transcriber

Privacy-first транскрипція робочих зустрічей на macOS. Один хоткей запускає запис, а після зустрічі в `notes/` з'являється Markdown-нотатка з темою, TL;DR, тезами, рішеннями, action items, відкритими питаннями та повним транскриптом із таймкодами й спікерами.

Запис, розпізнавання, діаризація та summary виконуються локально. За замовчуванням Ollama дозволена лише на localhost, тому аудіо й тексти не покидають Mac.

## Як це працює

```text
хоткей / SwiftBar
  └─ record.py
       ├─ мікрофон → «Я» або `LOCAL_N` у режимі «Динаміки»
       ├─ системний звук → співрозмовники
       └─ atomic session manifest
            └─ watch_and_process.py
                 1. перевірка WAV і mono 16 kHz processing-копії
                 2. MLX Whisper по обох доріжках
                 3. pyannote по системній доріжці
                 4. word-level speaker assignment і clock-drift correction
                 5. quality gates: periodic hallucinations, micro-clusters, дедуплікація
                 6. Ollama summary з кешем і map-reduce
                 7. атомарна notes/<сесія> — <тема>.md
                 8. ротація сирих WAV через 14 днів
```

Двотрекова архітектура дозволяє позначати вашу доріжку як «Я» без діаризації. Найкраща якість — у навушниках. Без навушників оберіть у стартовому popup режим «Динаміки» з Apple Voice Processing.

## Встановлення

```bash
chmod +x install.sh
./install.sh
```

Installer перевіряє macOS ≥ 14.2, встановлює ffmpeg і Python 3.12 через Homebrew, створює `.venv`, встановлює зафіксовані залежності, перевіряє Ollama, створює `.env` і реєструє LaunchAgents. Повторний запуск безпечний і використовується також для активації оновлених сервісів.

Після installer потрібно один раз:

1. Прийняти умови моделей `pyannote/segmentation-3.0` і `pyannote/speaker-diarization-community-1` на HuggingFace та записати read-токен у `.env`.
2. Повісити `request_record.sh` на хоткей у Raycast Script Command або Shortcuts → Run Shell Script.
3. Під час першого запису дозволити `python3.12` доступ до мікрофона і System Audio Recording. SwiftBar/хоткей лише передає команду постійному LaunchAgent і сам не відкриває мікрофон.
4. Додати Ollama.app у Login Items.

## Використання

- Хоткей або SwiftBar → вибір «Навушники» (Raw) чи «Динаміки» (AEC).
- Повторний виклик коректно закриває обидві доріжки та створює manifest зі станом `recorded`.
- Якщо обидві доріжки тихі 90 секунд, з'являється popup із пропозицією завершити запис. Timeout не зупиняє запис автоматично.
- Якщо обидва WAV не мають аудіосигналу, ASR, pyannote та Ollama не запускаються; створюється нотатка «Аудіосигнал відсутній».
- Watcher транскрибує чергу послідовно. Нотатка з'являється в `notes/` після завершення моделей.
- Записуйте лише за згодою учасників.

Коротші за `MIN_SESSION_SECONDS` записи не відправляються в ASR та LLM, щоб не генерувати галюцинації на тиші.

## Автостарт за активністю мікрофона

`mic_watch.py` кожні 4 секунди перевіряє через CoreAudio, чи використовується мікрофон. На початку дзвінка показується один діалог із вибором режиму:

- «Навушники» запускає чистий Raw-запис без Voice Processing;
- «Динаміки» запускає AEC і діаризацію людей біля локального мікрофона (`LOCAL_00`, `LOCAL_01`);
- «Пропустити» або 25 секунд без відповіді вимикає запит до завершення цього використання мікрофона;
- тихого запису без підтвердження немає.

Ручний виклик `request_record.sh` передає команду постійному `mic-autostart`
LaunchAgent, який показує той самий вибір і запускає запис у стабільному TCC-контексті.
Прямий `toggle_record.sh --raw` або `toggle_record.sh --aec` лишається для
діагностики та інтеграцій, яким уже надано доступ до мікрофона.

У режимі «Динаміки» pyannote обробляє обидві доріжки, тому ця стадія займає
більше часу. Модель завантажується один раз і повторно використовується для mic/sys.

Сервіс може реагувати також на Siri, диктування або голосові повідомлення. Керування:

```bash
tail -f logs/mic-autostart.log
launchctl bootout gui/$(id -u)/local.meeting-transcriber.mic-autostart
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.meeting-transcriber.mic-autostart.plist
```

## SwiftBar-індикатор

```bash
brew install --cask swiftbar
chmod +x meeting_rec.5s.sh
ln -s "$(pwd)/meeting_rec.5s.sh" ~/Documents/SwiftBar/
```

Вкажіть фактичну папку плагінів SwiftBar, якщо вона відрізняється. У менюбарі `🎙` означає очікування, а `🔴 12:34` — активний запис із таймером. SwiftBar не потребує доступу до мікрофона: команди виконує `mic-autostart` LaunchAgent.

## Конфігурація

Конфіг зберігається в `.env`. Файл містить секрет і створюється з правами `600`; не комітьте та не пересилайте його.

| Змінна | Default | Призначення |
|---|---|---|
| `HF_TOKEN` | — | HuggingFace read-токен для pyannote |
| `OLLAMA_MODEL` | `qwen3.6:27b-q4_K_M` | локальна модель для summary |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `OLLAMA_NUM_CTX` | `16384` | контекст моделі |
| `OLLAMA_THINK` | `false` | reasoning-режим |
| `ALLOW_REMOTE_OLLAMA` | `false` | явний opt-in для не-localhost endpoint |
| `MLX_MODEL` | `mlx-community/whisper-large-v3-mlx` | MLX Whisper model |
| `TRANSCRIBE_LANGUAGE` | `uk` | `uk` або `auto` для code-switching |
| `DIARIZE_DEVICE` | `auto` | MPS з автоматичним CPU fallback |
| `RECORD_AEC` | `false` | fallback для прямого запуску `record.py` без `--raw/--aec` |
| `DEDUP_MIC` | `true` | консервативна дедуплікація; при AEC вимикається |
| `MIN_SESSION_SECONDS` | `10` | мінімальна тривалість для ASR/LLM |
| `SILENT_RECORDING_PEAK_DBFS` | `-70` | цифрова тиша на обох доріжках → пропустити ASR/LLM |
| `MAX_AUTO_RETRIES` | `3` | автоматичні повтори з backoff |
| `MAX_RECORD_SECONDS` | `21600` | максимальна тривалість запису, 6 годин |
| `SILENCE_POPUP` | `true` | popup, коли обидві доріжки тривалий час тихі |
| `SILENCE_SECONDS` | `90` | тривалість тиші перед popup |
| `SILENCE_MIN_RECORD_SECONDS` | `120` | не показувати popup одразу після старту |
| `SILENCE_REPEAT_SECONDS` | `600` | інтервал повторного нагадування |
| `SILENCE_DIALOG_TIMEOUT` | `30` | timeout popup; запис продовжується |
| `MIC_ACTIVITY_DBFS` | `-42` | поріг активності мікрофона |
| `SYSTEM_ACTIVITY_DBFS` | `-50` | поріг активності системної доріжки |
| `ROTATE_DAYS` | `14` | вік WAV до видалення; `0` — не видаляти |

HF-токен передається бібліотекам через environment і не з'являється в аргументах процесів. Error logs додатково редагують рядки, схожі на токени.

## Watcher, помилки та повтори

```bash
tail -f logs/watcher.log
launchctl bootout gui/$(id -u)/local.meeting-transcriber.watcher
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.meeting-transcriber.watcher.plist
.venv/bin/python3 watch_and_process.py --once
.venv/bin/python3 watch_and_process.py --retry SESSION
.venv/bin/python3 transcribe.py recordings/SESSION --postprocess-only
.venv/bin/python3 watch_and_process.py --refresh-note SESSION
```

Останні дві команди повторно застосовують quality-фільтри до готового ASR-кешу
та оновлюють повний транскрипт у нотатці без повторного запуску моделей і summary.

Стадії обробки зберігаються в session manifest. Транзитні помилки повторюються автоматично з exponential backoff. Після вичерпання спроб сесія переходить у `terminal_failed`, а редагований traceback зберігається в `failed/<session>.log` з правами `600`.

ASR-кеш залежить від fingerprint аудіо, моделі та конфігурації. Часткові summary кешуються окремо, тому пізня помилка Ollama не змушує повторювати всю роботу.

## Тести

```bash
.venv/bin/python3 -m unittest discover -s tests -v
.venv/bin/python3 -m pip check
```

Перед регулярним використанням варто пройти ручний чекліст:

- [ ] після 90 секунд тиші обох доріжок popup зупиняє або продовжує запис відповідно до вибору;
- [ ] зустріч на 90+ хвилин;
- [ ] back-to-back зустрічі, поки перша ще обробляється;
- [ ] reboot → watcher, mic-autostart, Ollama і хоткей працюють;
- [ ] clock drift на 60+ хвилинах не перевищує прийнятний рівень після корекції.

## Типові проблеми

- **Python-оточення:** installer використовує перевірений Python 3.12 і зафіксовані версії пакетів.
- **Ollama в Docker:** на Mac зазвичай працює без Metal і значно повільніше. Використовуйте нативну Ollama.app.
- **TorchCodec:** не використовується — pyannote отримує вже декодований waveform.
- **Повільна діаризація:** перевірте `DIARIZE_DEVICE=auto`, доступність MPS і `logs/watcher.log`.
- **Дублікати без навушників:** у стартовому popup обирайте «Динаміки» (AEC).
- **Невідомі спікери:** велика частка `UNKNOWN` показується у quality warning фінальної нотатки.
- **Зміни сервісів не активувалися:** дочекайтеся завершення поточної сесії та повторно виконайте `./install.sh`.

## Файли проєкту

```text
record.py             атомарний двотрековий запис і session manifest
mic_watch.py          consent-діалог за активністю мікрофона
transcribe.py         preprocessing, ASR, diarization, sync і merge
watch_and_process.py  staged watcher, Ollama, retry та фінальна нотатка
pipeline_utils.py     atomic I/O, fingerprints і audio helpers
toggle_record.sh      тумблер запису
request_record.sh     безпечна черга команд SwiftBar/хоткея до LaunchAgent
meeting_rec.5s.sh     SwiftBar-плагін
install.sh            залежності та LaunchAgents
tests/                автоматичні тести
recordings/           сирі WAV і manifests
transcripts/          ASR JSON, кеші та зведені транскрипти
notes/                фінальні нотатки
failed/               редаговані error logs
```

## Поточні обмеження

- Імена для системних `SPEAKER_N` і локальних `LOCAL_N` мапляться вручну.
- Кілька людей біля одного пристрою погіршують діаризацію.
- AEC може приглушувати тихішого учасника при одночасній мові.
- Немає realtime-транскрипції, voice embeddings, пошуку по архіву чи серверної обробки.
