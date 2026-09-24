# REAL-ESTATE-BOT

## VPS foundation (PostgreSQL, Redis, Caddy)

The Docker foundation starts the stateful services required by the monitoring
orchestra. PostgreSQL and Redis are private to Docker; Caddy is the only
gateway and binds to `127.0.0.1:8080` until a production domain and access
policy are configured.

```sh
cp .env.example .env
# Edit .env: at minimum replace POSTGRES_PASSWORD and REDIS_PASSWORD.
docker compose --env-file .env config
docker compose up -d postgres redis caddy
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
./scripts/apply_migrations.sh
```

The migration script applies `001_init.sql` through
`005_orchestra_dispatcher.sql` in order. It records SHA-256 checksums in
`public.schema_migrations`, locks concurrent runs, and refuses an edited
already-applied migration. Use `docker compose down` for a normal stop; never
use `down -v` on a system containing needed data.

Future Telegram, controlled workers, and persistent browser services are
intentional disabled placeholders under the Compose `future` profile. Their
implementation must have bounded permissions and own health checks before it
is enabled. See [VPS hardening notes](docs/VPS_HARDENING.md) before deployment.

### Open Telegram control plane

The `telegram` Compose service receives only text and voice control messages.
It accepts messages from every Telegram user and chat, records each inbound
message with a unique `(chat_id, message_id)` idempotency key, and uses local
multilingual faster-whisper for voice notes. `/run`, `/pause`, `/resume`, and
`/cancel` require a short-lived `confirm <token>` response. A confirmed command
is durably queued for the Main Orchestra. The dispatcher validates a tiny
command grammar, selects a bounded acquisition plan, and writes the plan plus
audit records to PostgreSQL. It never launches a collector, browser, shell,
or unrestricted agent process itself.

After setting `TELEGRAM_TOKEN`, apply migrations before starting it:

```sh
./scripts/apply_migrations.sh
docker compose up -d --build telegram
docker compose logs -f telegram
```

### Main Orchestra dispatcher

The dispatcher runs inside the Telegram service and claims confirmed commands
from a PostgreSQL inbox with an expiring lease. A restart requeues only an
expired claim, and each Telegram confirmation message has a unique idempotency
key. It supports:

- `/run facebook-group(s) <https-url> [...]`: queues a 1–20 group Facebook
  batch using the dedicated connector.
- `/run website|instagram|tiktok <https-url>` and `/run facebook <https-url>`:
  queues a single Agent Reach-compatible run limited to five pages and 120
  seconds.
- `/pause source:<uuid>`, `/resume source:<uuid>`, and
  `/cancel batch:<uuid>|run:<uuid>|command:<uuid>|all`.

Planning and completing a command happen in one transaction, so a crash never
leaves a half-planned or duplicate batch. `/run` refuses a source that is
paused, disabled, retired, deleted, or waiting for human verification.
Cancelling a running Facebook batch stops the collector before its next group;
the group in progress finishes and the browser profile is handed back. A
running Agent Reach or Facebook item run is not cancelled directly: cancel its
batch instead. Every change is audited with the Telegram user as the actor.

An active, platform-matched browser profile must already be provisioned in
PostgreSQL. The dispatcher creates plans only; an operator-controlled one-shot
collector or Agent Reach invocation claims execution later. This is deliberate:
the Telegram bot cannot turn untrusted chat input into Docker, shell, or
browser launches.

### Future Supabase integration

Supabase is not connected by the monitoring foundation or its Telegram control
plane. The local Docker PostgreSQL database remains the primary database and
the migration runner is the only supported schema path today. `.env.example`
reserves `SUPABASE_URL`, `SUPABASE_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` for a
future server-side integration. Never expose the service-role key to a client,
browser, logs, or source control.

### Dedicated Facebook batch collector

The collector processes one already-queued `facebook_connector` batch at a
time. Migration `003_orchestration.sql` is authoritative: it permits at most
20 ordered groups per batch. The collector limits each group to 1–20 newest
post candidates (15 by default), uses the Browser Session Manager lease, and
inserts a short randomized pause between groups. It stops immediately on
checkpoint/login/CAPTCHA/account-warning signals, opens a verification job,
and releases the browser in `VERIFICATION_REQUIRED` state.

After `003` is applied and a batch/profile exist, run exactly one batch:

```sh
FACEBOOK_BATCH_ID=<queued-batch-uuid> docker compose --profile collector up --build facebook-collector
```

This service is intentionally not a daemon and contains no Agent Ridge,
Scrapling, analysis, or human-verification UI.

### Controlled Agent Reach adapter

The `agent-reach` Compose profile is a strictly bounded public-page reader for
future Orchestra fallback tasks. It uses an existing Browser Session Manager
lease and can only take explicit HTTPS targets for `facebook`, `instagram`,
`tiktok`, or `website`. It does not run Agent Reach's upstream CLI: that CLI
can install or execute tools and manage a browser, neither of which is an
acceptable privilege in this system. The adapter supports only
`read_public_page` and `extract_public_text`, visits at most five explicit
pages by default, has a 120-second total limit, never follows discovered
links, and exits for CAPTCHA, checkpoint, login, account-warning, or unusual
activity signals. It cannot join groups, send messages, or change accounts.

After a browser profile is provisioned, run one explicit task:

```sh
AGENT_REACH_TASK_JSON='{"task_id":"task-1","platform":"website","targets":["https://example.org"],"browser_profile_id":"website-main","browser_profile_name":"website-main"}' \
  docker compose --profile agent-reach run --rm agent-reach
```

The JSON result is normalized for the later analysis pipeline. A future
upstream integration must expose a read-only adapter compatible with this
policy; flipping an environment variable cannot enable it.

---

Telegram-бот, который ищет объекты недвижимости и потенциальных партнёров по
открытым источникам: разбирает запрос через LLM, ищет в собственном инстансе
SearXNG, читает найденные страницы, фильтрует результаты и присылает каждый
отдельным сообщением.

Материалы для BotFather (имя, описания, команды) — в [`bot_description.md`](bot_description.md).

---

## Как это работает

```
сообщение (текст или голос)
   │
   ├─ голос → STT (Whisper через OpenAI-совместимый API)
   │
   ├─ LLM: извлечение параметров        → ParsedQuery (локация, бюджет, площадь, язык…)
   ├─ QueryBuilder: 4–6 поисковых строк    на языке региона + английском
   ├─ SearXNG: параллельный поиск       → слияние и дедупликация по url_hash
   ├─ Supabase: отсев уже показанного
   ├─ Fetcher: загрузка и извлечение текста топ-N страниц
   ├─ LLM: оценка, фильтрация, структурирование
   └─ Supabase: сохранение (UNIQUE user_id + url_hash) → отправка пользователю
```

После извлечения параметров каждый этап **деградирует, а не падает**: не
работает парсер — ранжируем по сниппетам; не отвечает LLM-ранкер — отдаём
результаты поиска с пометкой; недоступен Supabase — результаты всё равно
отправляются, просто не запоминаются.

## Структура

```
bot/
├── main.py            точка входа: сборка сервисов, polling или webhook
├── config.py          все настройки из окружения (pydantic-settings v2)
├── handlers/          /start, текст, голос, кнопки, глобальный обработчик ошибок
├── keyboards/         только inline-клавиатуры
├── middlewares/       контекст логов, upsert пользователя, троттлинг
├── models/            Pydantic-модели: ParsedQuery, SearchHit, StructuredResult…
├── prompts/           тексты промптов (extract / rank / details)
├── services/
│   ├── llm/           абстракция провайдеров + менеджер с фолбэком
│   ├── stt/           распознавание речи, та же схема
│   ├── search/        клиент SearXNG и построитель запросов
│   ├── parser/        загрузка страниц и извлечение текста
│   ├── db/            репозиторий Supabase + SQL-миграция
│   └── pipeline.py    оркестрация всего сценария
└── utils/             нормализация URL и url_hash, работа с текстом

searxng/               полный снимок официального SearXNG (см. searxng/VENDOR.md)
docker/entrypoint.sh   запуск SearXNG и бота в одном контейнере
```

## Быстрый старт

### Docker (рекомендуется)

Один контейнер поднимает и SearXNG, и бота — ровно то же самое поедет на Render.

```sh
python scripts/setup_env.py   # спросит ключи и запишет .env
docker compose up --build
```

Скрипт читает ключи скрытым вводом (в терминале и в истории команд они не
остаются) и пишет `.env` с правами `0600`. Ничего никуда не отправляется —
ключи попадают только в локальный файл. Можно и вручную: `cp .env.example .env`
и заполнить `TELEGRAM_TOKEN` плюс ключ одного LLM-провайдера.

### Локально, без Docker

```sh
make install    # venv, зависимости, Chromium для резервного фетчера
make setup      # спросит ключи и запишет .env
make searxng    # терминал 1
make run        # терминал 2
```

То же самое руками, если make не нужен:

```sh
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt \
    -r searxng/requirements.txt -r searxng/requirements-server.txt
playwright install chromium

cp .env.example .env

# терминал 1 — SearXNG
SEARXNG_SECRET=dev-secret \
SEARXNG_SETTINGS_PATH=$PWD/searxng/settings/settings.yml \
PYTHONPATH=$PWD:$PWD/searxng \
granian --interface wsgi --host 127.0.0.1 --port 8888 searxng.api_only:application

# терминал 2 — бот
python -m bot.main
```

Проверить, что поиск отвечает:

```sh
curl -s 'http://127.0.0.1:8888/search?q=land+for+sale+cyprus&format=json' | head -c 400
```

## Локальная разработка

### Проверки

```sh
make check         # всё разом: снимок, lint, импорты, конфиг, миграция, гейт
make lint          # только ruff
make probe-gate    # только живой просмотр Facebook
make check-vendor  # только целостность вендоренного SearXNG
```

`make check-vendor` проверяет, что снимок SearXNG дошёл до репозитория целиком.
Проверка появилась не на пустом месте: правило `data/` в корневом `.gitignore`
(написанное для рабочего каталога бота) не было привязано к корню, а непривязанное
правило совпадает на любой глубине — и git молча не закоммитил
`searxng/searx/data/`. Это пакет из 16 файлов, который SearXNG импортирует при
старте, так что на свежем клоне поиск не поднимался вообще:
`ImportError: cannot import name 'data' from 'searx'`. На машине, где снимок
делали, всё работало — файлы просто лежали на диске. Ни lint, ни байт-компиляция
`searxng/` не трогают, поэтому не поймал никто. Теперь ловит эта проверка — и
заодно любое другое ignore-правило, дотягивающееся до снимка.

`make probe-gate` — единственная проверка здесь, которая гоняет настоящий код
по настоящему сценарию: поднимает заглушки вместо noVNC и websockify и дёргает
гейт так, как это сделал бы телефон (ссылка, страница, сокет, подпротокол,
кадры в обе стороны, закрытие). Браузер ей не нужен, идёт полсекунды, и именно
она поймала три ошибки, из-за которых ссылка из Telegram открывалась в пустоту.
Её же гоняет CI на каждый push.

`ruff format` в `make check` нет намеренно: дерево старше текущего
форматтера, 16 файлов не прошли бы. Это отдельная уборка, а не условие для
того, чтобы проверки вообще были.

### macOS на Apple Silicon

Повседневно бот запускается нативно — `make run`, безо всякого Docker, и
`make browsers` качает arm64-сборку Chromium. Быстро и без эмуляции.

Docker на M-процессоре — это про «проверить перед деплоем», а не про
повседневную работу: образ собирается под `linux/amd64`, потому что Google не
выпускает `google-chrome-stable` под arm64. Docker Desktop прогонит его через
Rosetta (включается в Settings → General → «Use Rosetta for x86/amd64
emulation», заметно быстрее) или через QEMU (медленно, по умолчанию).
Платформа уже закреплена в `docker-compose.yml`, отдельных флагов не нужно.

### Facebook локально

Локально ничего из серверной машинерии не нужно: ни Xvfb, ни x11vnc, ни noVNC,
ни туннеля. Без `FACEBOOK_CDP_URL` бот сам запускает браузер через Playwright и
просто открывает окно у вас на экране — входите и проходите проверку в нём.

Нужен установленный **Google Chrome** (обычное приложение в `/Applications`):
сессия Facebook запускается с `channel="chrome"`, то есть настоящим Chrome, а
не сборкой Chromium из Playwright — у настоящего браузера обычный отпечаток, и
для автоматизации Facebook это строго лучше. `make browsers` его не качает и
не должен.

```sh
FACEBOOK_ENABLED=true
FACEBOOK_HEADLESS=false                   # чтобы окно было видно
FACEBOOK_ADMIN_TELEGRAM_IDS=<ваш id>
FACEBOOK_GROUP_URLS=https://www.facebook.com/groups/...
# FACEBOOK_CDP_URL и FACEBOOK_DESKTOP_PUBLIC_BASE — не задавать
```

Профиль ложится в `./data/facebook_profile` и переживает перезапуски, так что
вход нужен один раз. `/facebook` в Telegram работает и здесь, только кнопки
«Открыть Facebook» не будет: `FACEBOOK_DESKTOP_PUBLIC_BASE` не задан, ссылку
выдавать не на что — бот так и напишет и предложит окно на этой машине. Это не
ошибка, а ровно тот случай, для которого писался запасной текст.

Прочитать реальную группу и посмотреть, что вышло:

```sh
python scripts/facebook_probe.py "<ссылка на группу>" "<поисковая фраза>"
```

### Что на Mac проверить нельзя

Xvfb, x11vnc и noVNC — линуксовые, и весь путь «кнопка в Telegram → токен →
живое окно на сервере» целиком собирается только там. Логику гейта закрывает
`make probe-gate`, всё остальное — только на VPS (или в Docker на линуксовой
машине). Зелёный `make check` про этот стек не говорит ничего.

## Настройка

Все параметры — в [`.env.example`](.env.example) с комментариями. Обязательны
только `TELEGRAM_TOKEN` и ключ одного LLM-провайдера.

### Смена LLM-провайдера

Меняется одной переменной. Все перечисленные провайдеры уже реализованы:

| `LLM_PROVIDER` | Ключ | Комментарий |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` | один ключ, большинство моделей |
| `groq` | `GROQ_API_KEY` | самый быстрый инференс |
| `together`, `fireworks` | `TOGETHER_API_KEY`, `FIREWORKS_API_KEY` | открытые модели |
| `anthropic` | `ANTHROPIC_API_KEY` | Claude, через Messages API |
| `openai` | `OPENAI_API_KEY` | |
| `nvidia` | `NVIDIA_API_KEY` | NIM / каталог NGC |
| `openai_compatible` | `LLM_API_KEY` + `LLM_BASE_URL` | vLLM, Ollama, LM Studio и любой другой совместимый endpoint |

Можно задать цепочку резерва — `LLM_FALLBACK_PROVIDERS=anthropic,groq`. Провайдер
без настроенного ключа просто пропускается, а не считается сбоем.

Разные модели на разные задачи:

```sh
LLM_MODEL_EXTRACT=openai/gpt-4o-mini              # дешёвая, разбирает запрос
LLM_MODEL_RANK=anthropic/claude-sonnet-4.5        # умная, ранжирует и пишет описания
```

### Добавление нового провайдера

Один файл и одна строка декоратора — остальной код не меняется.
Для OpenAI-совместимого API достаточно трёх атрибутов класса:

```python
# bot/services/llm/my_provider.py
from typing import ClassVar

from bot.services.llm.openai_compatible import OpenAICompatibleProvider
from bot.services.llm.registry import register_llm


@register_llm("my_provider")
class MyProvider(OpenAICompatibleProvider):
    name: ClassVar[str] = "my_provider"
    default_base_url: ClassVar[str | None] = "https://api.example.com/v1"
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("MY_PROVIDER_API_KEY",)
```

Импортируйте модуль в `bot/services/llm/__init__.py` — и `LLM_PROVIDER=my_provider`
работает. Для API с другим протоколом наследуйтесь от `LLMProvider` и реализуйте
`chat()`; `chat_structured()` со схемой и починкой невалидного JSON достанется
бесплатно. Речь (`bot/services/stt/`) устроена точно так же.

### Распознавание речи

```sh
STT_PROVIDER=groq_whisper           # или openai_whisper, nvidia
STT_MODEL=whisper-large-v3-turbo
```

Голосовые Telegram приходят в OGG/Opus, который принимают все перечисленные
endpoint'ы, — ffmpeg не нужен. `STT_ENABLED=false` вежливо отключает приём
голоса.

## Supabase

Применить миграцию (SQL-редактор Supabase или `psql`):

```sh
psql "$SUPABASE_DB_URL" -f bot/services/db/migrations/001_init.sql
```

Затем указать `SUPABASE_URL` и `SUPABASE_KEY` (**service_role** — на таблицах
включён RLS без разрешающих политик).

Таблицы: `users`, `searches`, `results`, `feedback`.
Защита от дубликатов — ограничение `UNIQUE (user_id, url_hash)` на `results`,
где `url_hash` = SHA-256 от нормализованного URL (`bot/utils/urls.py`: нижний
регистр хоста, без `www.`, без фрагмента, без `utm_*`/`fbclid`, отсортированные
параметры, `http` сведён к `https`). Ограничение действует на пользователя, а не
глобально, — два разных человека могут увидеть один и тот же объект, но каждый
только один раз.

**Без Supabase бот работает**, но: результаты не сохраняются, дедупликация между
сессиями не работает и **кнопки под результатами не показываются** — нажатие
некуда записать. При старте об этом пишется предупреждение.

## Деплой на VPS

Боевой вариант, если нужен Facebook. Браузер с залогиненным профилем должен
жить постоянно, а к нему в любой момент должен прийти человек с телефона —
когда Facebook попросит подтверждение. Render так не умеет: там имеет смысл
только «бот без Facebook» из раздела ниже.

### Какая машина нужна

| | минимум | рекомендуется |
|---|---|---|
| vCPU | 2 | 4 |
| RAM | 8 GB | 16 GB |
| Диск | 50 GB NVMe | 100 GB |

**Архитектура — обязательно x86_64 (amd64).** Google не собирает
`google-chrome-stable` под arm64, поэтому на ARM-машине образ просто не
соберётся; в `docker-compose.yml` платформа закреплена явно.

Почему два ядра — минимум. В момент проверки одно ядро занято Chrome, который
рисует страницу Facebook, второе — x11vnc, который кодирует картинку для
вашего телефона. На одном ядре живой просмотр начинает ощутимо тормозить ровно
тогда, когда по нему нужно попадать пальцем.

Почему 8 GB — минимум. Постоянно висят Chrome с профилем Facebook (0.8–1.5 GB),
SearXNG (~0.3 GB), бот (~0.2 GB), Xvfb с x11vnc (~0.1 GB); на пике добавляется
Chromium парсера — ещё до 1 GB. Если память кончится, ядро Linux убьёт самый
крупный процесс, то есть именно тот Chrome, в котором лежит залогиненная
сессия, и проверку придётся проходить заново.

**ОС:** чистый Ubuntu 24.04 LTS. Образы с панелями (CyberPanel, CloudPanel и
подобные) занимают порты 80/443 и память — они тут только мешают.

**Локация ЦОД:** выбирайте ближе к региону, из которого аккаунт Facebook
логинится обычно. Несовпадение географии IP — главная причина, по которой
checkpoint появляется чаще, чем хотелось бы. Ни один тариф от этого не
избавляет; смягчает только `PARSER_BROWSER_PROXY_URL` с резидентским прокси.

### 1. Docker

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
```

На машине с 8 GB стоит добавить swap — он не заменяет память, но даёт ядру
шанс вытеснить что-то неактивное вместо того, чтобы кого-то убить:

```sh
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 2. Код и `.env`

```sh
git clone https://github.com/infinitytechmain2024/REAL-ESTATE-BOT.git
cd REAL-ESTATE-BOT
python3 scripts/setup_env.py        # спросит ключи, запишет .env с правами 0600
```

Каталог `data/` переживает пересоздание контейнера — в нём лежат профиль
Chrome и файл токена. Контейнер работает не от root (uid 10001), а bind-mount
перекрывает права, выставленные при сборке, поэтому каталог нужно создать
заранее и отдать ему:

```sh
mkdir -p data && sudo chown 10001:10001 data
```

Без этого Chrome не сможет писать в профиль, и логин не переживёт ни одного
перезапуска.

### 3. Tailscale Funnel

Нужен только для Facebook: это то, что делает кнопку в Telegram открываемой
с любого телефона, не публикуя наружу ни одного своего порта.

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel --bg 8090     # именно 8090 — не 6080 и не порт CDP
sudo tailscale funnel status
```

Funnel включается для tailnet один раз в
[админке ACL](https://login.tailscale.com/admin/acls); если он ещё не включён,
команда сама напечатает нужный фрагмент. Адрес вида
`https://<машина>.<tailnet>.ts.net`, который она выведет, идёт в
`FACEBOOK_DESKTOP_PUBLIC_BASE`. После перезагрузки проверьте
`tailscale funnel status` — конфигурация обычно восстанавливается сама, но если
нет, команду нужно повторить (или положить в systemd-юнит).

### 4. Переменные Facebook

В `.env` (подробные комментарии — в [`.env.example`](.env.example)):

```sh
FACEBOOK_ENABLED=true
FACEBOOK_ADMIN_TELEGRAM_IDS=123456789          # кому придёт кнопка
FACEBOOK_DESKTOP_PUBLIC_BASE=https://<машина>.<tailnet>.ts.net
FACEBOOK_GROUP_URLS=https://www.facebook.com/groups/...
# FACEBOOK_DESKTOP_PIN=1234                    # ещё один рубеж перед живым просмотром
```

Двухфакторную аутентификацию на рабочем аккаунте лучше выключить — она убирает
из входа шаг с кодом. Checkpoint она не убирает: ради него всё это и сделано.

### 5. Запуск

```sh
docker compose up -d --build        # первая сборка долгая: ставится Chrome
docker compose logs -f
```

В логах при `FACEBOOK_ENABLED=true` должны появиться строки `entrypoint:`
про Xvfb, Chrome, x11vnc и noVNC — если какой-то из них нет, дальше смотреть
нечего, Facebook работать не будет.

### 6. Проверка

Сначала — путь «ссылка из Telegram → живой браузер», без Chrome и без телефона:

```sh
docker compose exec bot python scripts/gate_probe.py
```

Скрипт поднимает заглушки вместо noVNC и websockify и дёргает гейт так, как это
сделал бы телефон. Все проверки должны пройти. Это дешевле, чем выяснять, что
ссылка не открывается, в момент, когда checkpoint уже висит.

Потом — по-настоящему: отправьте боту `/facebook`, нажмите кнопку и убедитесь,
что окно Chrome видно и на нажатия реагирует. Сделайте это **до** того, как
понадобится, а не после.

### Лимиты ресурсов

`docker-compose.yml` ограничивает контейнер: `mem_limit`, `cpus` и `shm_size`.
Значения рассчитаны на 4 vCPU / 16 GB; для 2 vCPU / 8 GB в комментарии рядом
указаны другие. Смысл в том, чтобы предел выставил Docker — раньше, чем до
процессов доберётся OOM killer ядра и убьёт залогиненный Chrome.

`shm_size` отдельно: Docker по умолчанию монтирует `/dev/shm` размером 64 MB,
а Chrome держит там разделяемую память между своими процессами. При прокрутке
группы с картинками этого не хватает, и вкладки начинают умирать с «Target
closed» на вид случайно, при свободной памяти на хосте.

### Что смотрит наружу

Ничего, что нужно открывать самому. Бот работает в режиме polling — входящие
подключения ему не нужны; SearXNG, noVNC, x11vnc и порт CDP слушают только
`127.0.0.1`. Единственная дверь снаружи — гейт на 8090, и до него доходит
только трафик через Tailscale Funnel. Поэтому firewall можно закрыть целиком,
кроме SSH:

```sh
sudo ufw allow OpenSSH && sudo ufw enable
```

Никогда не выставляйте наружу 6080 (noVNC) или порт CDP: первый пускает к
браузеру без токена, второй — это полное управление браузером по HTTP без
всякой аутентификации.

### Обслуживание

```sh
docker compose logs -f --tail=100        # логи
docker compose restart bot               # перезапуск без пересборки
git pull && docker compose up -d --build # обновление
tar czf fb-profile.tgz data/             # бэкап профиля (в нём живая сессия)
```

Бэкап профиля стоит снять сразу после того, как вы первый раз прошли вход и
проверку: восстановить его быстрее, чем проходить checkpoint заново.

## Деплой на Render

Вариант для бота без Facebook. Постоянно живого браузера, в который можно
зайти с телефона, здесь не получится — для этого нужен раздел выше.

[`render.yaml`](render.yaml) описывает один Docker-сервис с обоими процессами.

1. New → Blueprint, указать репозиторий.
2. Заполнить переменные, помеченные `sync: false` (токен, ключи, Supabase).
3. Deploy.

По умолчанию это **background worker** в режиме polling: публичный URL не нужен,
а API SearXNG остаётся на loopback, недоступный извне. Фоновые воркеры доступны
только на платных планах.

Для webhook закомментируйте worker и раскомментируйте web-сервис в конце
`render.yaml`, затем задайте `TELEGRAM_WEBHOOK_URL` = адрес сервиса. Наружу
торчит webhook-сервер бота, SearXNG по-прежнему на `127.0.0.1`.

## SearXNG

`searxng/` — полный снимок официального репозитория, коммит и все локальные
изменения зафиксированы в [`searxng/VENDOR.md`](searxng/VENDOR.md).

Требование «только backend + JSON API» выполнено на уровне маршрутизации, а не
удалением файлов, — чтобы снимок можно было обновлять:

- `searxng/settings/settings.yml` задаёт `search.formats: [json]`. В upstream
  стоит `[html]`, при котором `/search?format=json` отдаёт 403.
- `searxng/api_only.py` — WSGI-обёртка, пропускающая только `/search`,
  `/healthz`, `/config`, `/stats` и `/metrics`. На `/`, `/preferences`,
  `/about`, `/static/*`, `/autocompleter` и `/image_proxy` возвращается 404.

Движки: Google, Bing, DuckDuckGo, Brave, Startpage, Mojeek, Qwant, Wikipedia,
Wikidata. Первые четыре и Qwant с Mojeek в upstream выключены по умолчанию,
поэтому включены явно и взвешены — универсальные поисковики выше
энциклопедических.

Обновление снимка — по инструкции в `VENDOR.md`.

## Полезные команды

## Browser session manager

`browser` owns persistent, non-headless Chromium contexts for future controlled
collectors. It is not a scraper and has no published host port. A caller on the
private Docker network must use `Authorization: Bearer $BROWSER_SESSION_API_TOKEN`
to acquire a profile, release its opaque session token, or request a screenshot.
Redis leases and kernel filesystem locks ensure one live browser per profile;
leases expire after a process crash. Profile data and screenshots are stored in
private named Docker volumes with owner-only permissions.

```sh
docker compose up -d --build browser
docker compose logs -f browser
```

Set `BROWSER_SESSION_API_TOKEN` to a unique long secret before starting it.
The persisted states remain the migration-003 values (`ready`, `in_use`,
`human_verification_required`, etc.); `LOCKED` and `COOLDOWN` are transient
operational API conditions and are never written to the database.

```sh
make help          # список целей
make setup         # спросить ключи и записать .env
make install       # venv, зависимости и браузер
make browsers      # только Chromium для Playwright
make run           # бот локально
make searxng       # SearXNG локально
make lint          # ruff
make probe-gate    # живой просмотр Facebook, целиком
make check-vendor  # целостность вендоренного SearXNG
make check         # снимок, lint, импорты, конфиг, миграция, гейт
make check-api     # SearXNG JSON API (SearXNG должен быть запущен)
make docker-up     # то же, что на Render
```

## Лицензии

Код бота — в этом репозитории. `searxng/` распространяется под AGPL-3.0-or-later
(см. `searxng/LICENSE`) и является немодифицированным снимком стороннего проекта.
