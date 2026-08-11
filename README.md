# Meeting Transcriber

Локальний сервіс для транскрипції зустрічей на macOS. Він збирає captions із
Google Meet або записує звук, створює українську Markdown-нотатку, окремо
оцінює кандидатів і за потреби додає задачі в Notion.

Основна обробка виконується на Mac: Whisper/pyannote працюють локально, а текст
аналізує локальна Ollama. Віддалений Ollama endpoint заблокований, доки явно не
встановлено `ALLOW_REMOTE_OLLAMA=true`.

Runtime-дані та логи створюються з приватними правами. Автоматичний Meet import
обмежує розмір, кількість реплік, складність fuzzy matching і час нормалізації;
некоректні exports переносяться в `failed/` замість нескінченних повторів.

## Що вміє

- імпортувати live captions, спікерів і чат із Google Meet;
- записувати мікрофон і системний звук із подальшими ASR та діаризацією;
- створювати TL;DR, тези, рішення, action items і повний транскрипт;
- оцінювати співбесіди українською та визначати підтверджений рівень кандидата;
- створювати action items і задачі на фідбек у наявній Notion-дошці.

Результати зберігаються локально:

```text
notes/                  звичайні нотатки зустрічей
transcripts/            очищені транскрипти, evidence та технічні кеші
candidate_evaluations/  приватні звіти про кандидатів
recordings/             WAV і manifests аудіозаписів
failed/                 логи невдалих обробок
```

## Встановлення

Потрібні macOS 14.2 або новіша та [Homebrew](https://brew.sh).

```bash
chmod +x install.sh
./install.sh
```

Installer налаштує Python, ffmpeg, залежності, `.env` і фонові сервіси. Під час
першого запуску він запропонує вибрати потрібні модулі. Повторний запуск
безпечний і застосовує оновлення сервісів.

Після встановлення:

1. Запустіть Ollama.app і додайте її в Login Items.
2. Для Google Meet завантажте `chrome-extension/` через
   `chrome://extensions` → **Developer mode** → **Load unpacked**.
3. Для аудіозапису прийміть умови моделей pyannote, додайте HuggingFace
   read-токен у `.env` і надайте macOS доступ до мікрофона та System Audio
   Recording під час першого запуску.

## Модулі

Сервіс складається з трьох незалежних частин:

| Модуль | Що контролює |
|---|---|
| `audio` | запис звуку, транскрипцію та діаризацію |
| `candidates` | автоматичну оцінку співбесід |
| `notion` | створення задач у Notion |

Керування:

```bash
.venv/bin/python3 modules.py status
.venv/bin/python3 modules.py configure

.venv/bin/python3 modules.py disable audio
.venv/bin/python3 modules.py enable candidates notion
```

Зміни одразу записуються в `.env` і застосовуються до фонових сервісів.
Вимкнення `audio` блокує нові записи й залишає наявні WAV у черзі. Google Meet,
оцінка текстових транскриптів і локальні нотатки продовжують працювати.

## Google Meet — рекомендований режим

Chrome-розширення читає live captions, імена спікерів і доступний чат без
запису аудіо чи відео. Після завершення зустрічі воно експортує JSON у
`~/Downloads`, а watcher автоматично створює транскрипт і нотатку.

Як користуватися:

1. У Meet переконайтеся, що віджет показує **«Запис RTC captions»** або
   **«Запис captions»**.
2. Для резервного DOM-режиму один раз виберіть **CC → Українська**.
3. Вийдіть із зустрічі або закрийте вкладку — решта відбудеться автоматично.

Після успішного імпорту JSON видаляється з Downloads. У разі помилки він
залишається для повторної спроби. Повніший повторний export тієї самої сесії
оновлює транскрипт і нотатку; невідновний export переноситься в `failed/`.
RTC-протокол Meet не документований. DOM fallback вмикається, якщо RTC-канал
не відкрився за 20 секунд або пакети стабільно не декодуються; відсутність
мовлення у відкритому каналі не активує fallback. Оновлення однієї RTC-репліки
збираються за `messageId` і `messageVersion`, тому старі версії не дублюють текст.

Якщо Chrome зберігає downloads не в `~/Downloads`, задайте фактичний шлях у
`MEET_DOWNLOADS_DIR`. Після оновлення коду розширення натисніть **Reload** на
`chrome://extensions` і перезавантажте відкриту вкладку Meet.

Ручний імпорт:

```bash
.venv/bin/python3 meet_import.py ~/Downloads/meet-....json
```

Додайте `--no-summary`, щоб створити лише транскрипт, або `--force`, щоб
свідомо повторити імпорт.

## Перевірка висновків

Звичайна нотатка формується з двох evidence-проходів: окремо збираються
рішення/дії/відкриті питання та окремо контекст. Після цього текстовий
reasoning-прохід хронологічно перевіряє прийняття, заперечення й пізніші зміни,
а окремий прохід без reasoning формує валідний JSON. Фінальний summary
будується лише з узгодженого ledger.

У `Рішення` потрапляють лише явно підтверджені домовленості, у `Action items` —
лише відкриті зобов'язання. Надіслане під час зустрічі посилання чи контакт не
створює задачу в Notion. Перевірені підстави зберігаються приватно у
`transcripts/<session>/summary-evidence.json` і не додаються до нотатки.
Критичні operational items не обрізаються загальним лімітом. Перший аналіз
довший за однопрохідний summary; прогрес видно в watcher log, а завершені етапи
кешуються.

## Аудіозапис

Аудіомодуль записує дві окремі доріжки: мікрофон і системний звук. Запуск через
`request_record.sh` пропонує один із режимів:

- **Навушники** — локальний голос позначається як «Я»;
- **Динаміки** — люди біля мікрофона діаризуються як `LOCAL_00`, `LOCAL_01`, а
  дублікати із системної доріжки прибираються.

Повторний виклик завершує запис. За замовчуванням автоматичне стеження за
мікрофоном вимкнене (`MIC_AUTO_START=false`), тому запуск дзвінка сам по собі
не починає запис і не показує popup.

Для хоткея прив’яжіть `request_record.sh` у Raycast або Shortcuts. Прямий
діагностичний запуск:

```bash
./toggle_record.sh --raw
./toggle_record.sh --speakers
```

Опційний індикатор SwiftBar — `meeting_rec.5s.sh`; додайте його у свою папку
SwiftBar plugins.

Записуйте лише за згодою учасників. Цифрова тиша та надто короткі записи не
відправляються в ASR, діаризацію чи LLM.

## Оцінка кандидатів

Skill оцінювання не входить до публічного репозиторію. Щоб додати власний,
покладіть його в локальну папку `skills/candidate-evaluation/`:

```text
skills/candidate-evaluation/
├── SKILL.md
├── assets/report_template.md
└── references/
    ├── anchors.md
    ├── bias_checklist.md
    ├── decision_policy.md
    ├── level_anchors.md
    └── runtime_prompt.md
```

Після цього ввімкніть модуль командою
`.venv/bin/python3 modules.py enable candidates`. Папка skill-а ігнорується
Git, тому її вміст залишається локальним. Без власного skill-а модуль вимкнений
за замовчуванням.

Автоматична оцінка запускається лише для безпечного явного формату назви з
keyword `hiring`, `interview` або `networking`:

Рекомендований формат назви:

```text
Interview | Jane Doe | Senior Data Engineer | Hiring Manager
```

Перша частина обирає candidate flow, друга є іменем кандидата, а довільна третя
частина задає цільову роль і рівень. Необов’язкова четверта частина задає етап:
`HR/Recruiter`, `Hiring Manager`, `Technical` або `Final`. Без цільового рівня
сервіс усе одно визначить demonstrated level за `CANDIDATE_LEVELS`.
На нефінальних етапах звіт рекомендує продовжити, зупинити або провести цільову
перевірку, але не підмінює це фінальним рішенням про найм.
Неоднозначні назви на кшталт `Customer interview — churn research` створюють
звичайну нотатку, а не помилковий candidate report.

Перед оцінюванням candidate flow визначає стан співбесіди: повноцінна,
достроково завершена, недостатньо змісту, скасована/no-show або технічна
проблема. Лише повноцінна співбесіда запускає scorecard і reasoning.

Оцінювання використовує reasoning окремо від звичайних нотаток. Він вмикається
для фінального verdict; механічне витягування цитат працює швидше без reasoning:

```dotenv
OLLAMA_THINK=false
SUMMARY_EXTRACT_THINK=false
SUMMARY_RECONCILE_THINK=true
CANDIDATE_OLLAMA_THINK=true
CANDIDATE_LEVELS=Junior,Middle,Senior,Lead
CANDIDATE_TARGET_LEVEL= # роль і рівень, наприклад Junior Data Analyst
```

Ручний повторний аналіз готового транскрипту:

```bash
.venv/bin/python3 watch_and_process.py --evaluate-candidate SESSION
```

Повторна обробка тієї самої сесії не дублює звіт або Notion-задачу.
Якщо кандидат відмовився до основної частини або зустріч не відбулася, сервіс
зберігає короткий процесний результат без балів і грейду та не створює задачу
на hiring feedback.

## Notion

Модуль використовує наявну Notion-інтеграцію. У `.env` мають бути заповнені:

```dotenv
NOTION_API_KEY=ntn_...
NOTION_DATA_SOURCE_ID=...
```

Коли `notion` увімкнено:

- action items нових нотаток читаються зі структурованого evidence ledger і
  створюються зі статусом `Inbox`; для старих та ручних нотаток залишається
  Markdown fallback;
- після співбесіди створюється задача
  `Дати фідбек по кандидату: <candidate>`;
- повний транскрипт і звіт кандидата в Notion не передаються;
- повторний запуск не створює дублікатів.

Перевірка без API-запису та ручна синхронізація:

```bash
.venv/bin/python3 notion_agent.py
.venv/bin/python3 notion_agent.py --apply
```

Помилка Notion не скасовує локальну нотатку або оцінку. Невідправлені задачі
повторюються автоматично; candidate-feedback зберігається в локальній черзі, а
sync state — у двох атомарних копіях.

## Основні налаштування

Конфіг зберігається в `.env`. Він може містити секрети, тому не комітьте й не
пересилайте його. Повний перелік зі значеннями за замовчуванням є в
`.env.example`.

| Змінна | Призначення |
|---|---|
| `OLLAMA_MODEL` | модель для summary та оцінювання |
| `SUMMARY_EXTRACT_THINK` | reasoning під час збору evidence; зазвичай `false` |
| `SUMMARY_RECONCILE_THINK` | reasoning лише для рішень і суперечностей |
| `SUMMARY_CRITICAL_MERGE_NUM_PREDICT` | ліміт відповіді для великого critical-evidence JSON merge; default `8192` |
| `SUMMARY_CRITICAL_RECONCILE_NUM_PREDICT` | ліміт JSON після reasoning-узгодження; default `8192` |
| `CANDIDATE_OLLAMA_THINK` | reasoning лише для оцінки кандидатів |
| `MEET_AUTO_IMPORT` | автоматичний імпорт Meet JSON |
| `MEET_AUTO_SUMMARY` | автоматичне створення нотатки після імпорту |
| `NOTION_TASK_OWNER_NAMES` | імена/аліаси власника; на борду потрапляють лише його action items |
| `TRANSCRIBE_LANGUAGE` | `uk` або `auto` для аудіо-ASR |
| `MIC_AUTO_START` | автоматично реагувати на активний мікрофон |
| `MAX_AUTO_RETRIES` | кількість спроб; default `8` дає близько двох годин retry-вікна |
| `ROTATE_DAYS` | через скільки днів видаляти завершені WAV; `0` — ніколи |

Перемикачі `AUDIO_PIPELINE_ENABLED`, `CANDIDATE_EVALUATION_ENABLED` і
`NOTION_SYNC_ENABLED` краще змінювати через `modules.py`.

## Стан, логи та повторні спроби

```bash
# стан модулів і сервісів
.venv/bin/python3 modules.py status

# прогрес обробки
tail -f ~/Library/Logs/MeetingTranscriber/watcher.log
tail -f ~/Library/Logs/MeetingTranscriber/mic-autostart.log

# один цикл watcher-а
.venv/bin/python3 watch_and_process.py --once

# повторити невдалу аудіосесію
.venv/bin/python3 watch_and_process.py --retry SESSION

# повторити невдалий Meet summary з локального транскрипту
.venv/bin/python3 watch_and_process.py --retry-meet SESSION
```

Транзитні помилки аудіо та Meet повторюються автоматично; Meet retry не
залежить від наявності export-файлу в Downloads. Після вичерпання спроб деталі
зберігаються в `failed/<session>.log` без токенів. Якщо зміни сервісів не
активувалися, виконайте:

```bash
.venv/bin/python3 modules.py apply
```

## Тести

```bash
.venv/bin/python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.js
.venv/bin/python3 -m pip check
```
