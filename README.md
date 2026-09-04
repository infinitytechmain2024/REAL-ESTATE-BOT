# REAL-ESTATE-BOT

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
│   ├── db/            репозиторий Supabase + SQL-миграции
│   ├── limits.py      суточные квоты на пользователя
│   ├── costs.py       учёт стоимости LLM и дневной hard-limit
│   └── pipeline.py    оркестрация всего сценария
└── utils/             нормализация URL, url_hash, проверка адресов (SSRF)

tests/                 юнит-тесты (`make test`)

searxng/               полный снимок официального SearXNG (см. searxng/VENDOR.md)
docker/entrypoint.sh   запуск SearXNG и бота в одном контейнере
```

## Быстрый старт

### Docker (рекомендуется)

Один контейнер поднимает и SearXNG, и бота — ровно то же самое поедет на Render.

```sh
cp .env.example .env        # укажите TELEGRAM_TOKEN и ключ одного LLM-провайдера
docker compose up --build
```

### Локально, без Docker

```sh
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r searxng/requirements.txt -r searxng/requirements-server.txt

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
psql "$SUPABASE_DB_URL" -f bot/services/db/migrations/002_limits_and_costs.sql
```

Затем указать `SUPABASE_URL` и `SUPABASE_KEY` (**service_role** — на таблицах
включён RLS без разрешающих политик).

Таблицы: `users`, `searches`, `results`, `feedback`, `daily_usage`, `llm_usage`.
Защита от дубликатов — ограничение `UNIQUE (user_id, url_hash)` на `results`,
где `url_hash` = SHA-256 от нормализованного URL (`bot/utils/urls.py`: нижний
регистр хоста, без `www.`, без фрагмента, без `utm_*`/`fbclid`, отсортированные
параметры, `http` сведён к `https`). Ограничение действует на пользователя, а не
глобально, — два разных человека могут увидеть один и тот же объект, но каждый
только один раз.

**Без Supabase бот работает**, но: результаты не сохраняются, дедупликация между
сессиями не работает и **кнопки под результатами не показываются** — нажатие
некуда записать. Суточные квоты в этом случае считаются в памяти процесса и
сбрасываются при перезапуске. При старте об этом пишется предупреждение.

## Лимиты и расходы

Один запрос стоит нескольких вызовов LLM и до десятка загрузок страниц, поэтому
расход ограничен на четырёх уровнях. Все счётчики суток сбрасываются в 00:00 UTC.

| Ограничение | Переменная | По умолчанию | Что делает |
|---|---|---|---|
| Пауза между запросами | `TELEGRAM_REQUEST_COOLDOWN_SECONDS` | 3 с | «не так часто» |
| Пауза между «Подробнее» | `TELEGRAM_DETAILS_COOLDOWN_SECONDS` | 15 с | единственная платная кнопка |
| Параллельно на пользователя | `TELEGRAM_MAX_SEARCHES_PER_USER` | 1 | «не так много сразу» |
| Параллельно всего | `TELEGRAM_MAX_CONCURRENT_SEARCHES` | 3 | защита воркера |
| Поисков в сутки | `LIMITS_DAILY_SEARCHES` | 50 | на пользователя |
| Сводок «Подробнее» в сутки | `LIMITS_DAILY_DETAILS` | 100 | на пользователя |
| Бюджет LLM в сутки | `LIMITS_DAILY_COST_USD` | 5.0 | на всё развёртывание |
| Таймаут пайплайна | `PIPELINE_TIMEOUT_SECONDS` | 90 с | вместо зависания |

Стоимость каждого вызова считается как «токены × цена» из
`LLM_PRICE_PROMPT_USD_PER_1M` / `LLM_PRICE_COMPLETION_USD_PER_1M` и пишется в
таблицу `llm_usage`. **Цены нужно задать под свою модель** — по умолчанию там
прайс gpt-4o-mini, и с другой моделью лимит сработает не на той сумме.

Когда дневной бюджет исчерпан, пайплайн останавливается, а пользователям из
`TELEGRAM_ADMIN_IDS` уходит одно уведомление. Текущая сумма при старте
восстанавливается из `llm_usage`, поэтому перезапуск не выдаёт новый бюджет.

## Безопасность загрузки страниц

Бот скачивает произвольные URL, выданные поисковиком, поэтому перед каждым
запросом — **и перед каждым редиректом** — адрес проверяется
(`bot/utils/net.py`): разрешены только `http`/`https` и только публично
маршрутизируемые адреса. Приватные диапазоны (RFC1918), loopback,
link-local `169.254.0.0/16` (метаданные облака), CGNAT и IPv4-адреса,
завёрнутые в IPv6, отклоняются. Редиректы обрабатываются вручную именно
поэтому: httpx проверял бы только первый URL.

Остаточный риск — DNS rebinding: имя резолвится при проверке и ещё раз при
подключении. Закрыть это полностью можно только подключением по уже
проверённому IP с подменой SNI, что httpx не позволяет сделать чисто.

## Деплой на Render

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

```sh
make help          # список целей
make install       # venv и зависимости
make run           # бот локально
make searxng       # SearXNG локально
make install-dev   # плюс pytest и ruff
make test          # юнит-тесты
make lint          # ruff
make check         # линт, импорты, конфиг, миграции, тесты
make docker-up     # то же, что на Render
```

## Лицензии

Код бота — в этом репозитории. `searxng/` распространяется под AGPL-3.0-or-later
(см. `searxng/LICENSE`) и является немодифицированным снимком стороннего проекта.
