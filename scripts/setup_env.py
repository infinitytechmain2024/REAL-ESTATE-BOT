#!/usr/bin/env python3
"""Interactive .env builder.

Answers the question "where do I put the bot token?" without anyone having to
know the file format. Run it, answer the prompts, get a working .env::

    python3 scripts/setup_env.py

Secrets are read with getpass, so they are never echoed to the terminal and
never land in shell history. The finished file is written with owner-only
permissions and every value is masked when the summary is printed.

Nothing here talks to the network. The keys go from your keyboard into a file
on this machine and nowhere else.
"""

from __future__ import annotations

import re
import stat
import sys
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Loose on purpose: these catch a pasted-the-wrong-thing mistake, not every
# malformed key. A provider changing its prefix must not block setup.
TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

# provider -> (label, env var for its key, suggested model)
LLM_PROVIDERS: dict[str, tuple[str, str, str]] = {
    "openrouter": ("OpenRouter -- один ключ, все модели", "OPENROUTER_API_KEY", "openai/gpt-4o-mini"),
    "groq": ("Groq -- самый быстрый", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "openai": ("OpenAI", "OPENAI_API_KEY", "gpt-4o-mini"),
    "anthropic": ("Anthropic (Claude)", "ANTHROPIC_API_KEY", "claude-sonnet-4-5"),
    "nvidia": ("NVIDIA NIM", "NVIDIA_API_KEY", "meta/llama-3.3-70b-instruct"),
}

STT_PROVIDERS: dict[str, tuple[str, str, str]] = {
    "groq_whisper": ("Groq Whisper -- есть бесплатный лимит", "GROQ_API_KEY", "whisper-large-v3-turbo"),
    "openai_whisper": ("OpenAI Whisper", "OPENAI_API_KEY", "whisper-1"),
}


@dataclass
class Answers:
    """Everything collected from the prompts, in the order it is written out."""

    telegram_token: str = ""
    llm_provider: str = "openrouter"
    llm_model: str = ""
    llm_key_var: str = ""
    llm_key: str = ""
    stt_enabled: bool = False
    stt_provider: str = ""
    stt_model: str = ""
    stt_key_var: str = ""
    stt_key: str = ""
    supabase_url: str = ""
    supabase_key: str = ""
    extra: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# prompt helpers
# --------------------------------------------------------------------------


def say(text: str = "") -> None:
    print(text, file=sys.stderr)


def ask(prompt: str, *, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def ask_secret(prompt: str, *, required: bool = True) -> str:
    """Read a secret without echoing it."""
    while True:
        try:
            value = getpass(f"{prompt} (ввод скрыт): ").strip()
        except EOFError:
            return ""
        if value or not required:
            return value
        say("  Пустое значение. Попробуйте ещё раз или Ctrl+C для выхода.")


def ask_yes_no(prompt: str, *, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})").lower()
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


def choose(prompt: str, options: dict[str, tuple[str, str, str]], default: str) -> str:
    say()
    say(prompt)
    keys = list(options)
    for index, key in enumerate(keys, start=1):
        marker = " (по умолчанию)" if key == default else ""
        say(f"  {index}. {options[key][0]}{marker}")
    raw = ask("Номер или имя", default=default)

    if raw.isdigit() and 1 <= int(raw) <= len(keys):
        return keys[int(raw) - 1]
    if raw in options:
        return raw
    say(f"  Не распознал {raw!r}, беру {default}.")
    return default


def mask(secret: str) -> str:
    """Show just enough to recognise a key without revealing it."""
    if not secret:
        return "(не задан)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-4:]} ({len(secret)} символов)"


# --------------------------------------------------------------------------
# the interview
# --------------------------------------------------------------------------


def collect() -> Answers:
    answers = Answers()

    say("=" * 68)
    say("  Настройка .env для REAL-ESTATE-BOT")
    say("=" * 68)
    say()
    say("Ключи вводятся скрыто и записываются только в локальный файл .env.")
    say("Никуда не отправляются. Enter принимает значение в скобках.")

    # --- Telegram ---------------------------------------------------------
    say()
    say("--- Telegram ---")
    say("Токен выдаёт @BotFather по команде /newbot.")
    while True:
        token = ask_secret("Токен бота")
        if TELEGRAM_TOKEN_RE.match(token):
            break
        say("  Непохоже на токен Telegram (ожидается 123456789:AAH...).")
        if ask_yes_no("  Всё равно использовать?"):
            break
    answers.telegram_token = token

    # --- LLM --------------------------------------------------------------
    provider = choose("--- Языковая модель ---", LLM_PROVIDERS, "openrouter")
    label, key_var, suggested = LLM_PROVIDERS[provider]
    answers.llm_provider = provider
    answers.llm_key_var = key_var
    answers.llm_model = ask("Модель", default=suggested)
    say(f"Ключ берётся из {key_var}.")
    answers.llm_key = ask_secret(f"Ключ {label.split(' --')[0]}")

    if provider == "openrouter":
        say()
        say("  Напоминание: пополните баланс на openrouter.ai.")
        say("  Пустой счёт выглядит как поломка бота, а не как проблема с оплатой.")

    # --- speech to text ---------------------------------------------------
    say()
    say("--- Голосовые сообщения ---")
    say("Можно включить позже: STT_ENABLED в .env.")
    if ask_yes_no("Включить распознавание речи?", default=False):
        stt = choose("Провайдер распознавания", STT_PROVIDERS, "groq_whisper")
        stt_label, stt_key_var, stt_model = STT_PROVIDERS[stt]
        answers.stt_enabled = True
        answers.stt_provider = stt
        answers.stt_key_var = stt_key_var
        answers.stt_model = ask("Модель распознавания", default=stt_model)
        if stt_key_var == answers.llm_key_var:
            say(f"  {stt_key_var} уже задан выше — переиспользую.")
        else:
            answers.stt_key = ask_secret(f"Ключ {stt_label.split(' --')[0]}")

    # --- Supabase ---------------------------------------------------------
    say()
    say("--- Supabase ---")
    say("Без неё бот ищет и отвечает, но не запоминает результаты")
    say("и не показывает кнопки под ними. Для первого теста можно пропустить.")
    if ask_yes_no("Подключить Supabase?", default=False):
        answers.supabase_url = ask("Project URL (https://xxx.supabase.co)")
        say("Нужен service_role, не anon: на таблицах включён RLS.")
        answers.supabase_key = ask_secret("service_role key")

    return answers


# --------------------------------------------------------------------------
# writing the file
# --------------------------------------------------------------------------


def render(answers: Answers) -> str:
    """Build the .env body. Only what was answered; the rest keeps its defaults."""
    lines = [
        "# Создан scripts/setup_env.py. Полный список настроек -- в .env.example.",
        "# Этот файл содержит секреты и не попадает в git (см. .gitignore).",
        "",
        "ENVIRONMENT=local",
        "LOG_LEVEL=INFO",
        "LOG_FORMAT=console",
        "",
        "# --- Telegram ---",
        f"TELEGRAM_TOKEN={answers.telegram_token}",
        "TELEGRAM_MODE=polling",
        "",
        "# --- Языковая модель ---",
        f"LLM_PROVIDER={answers.llm_provider}",
        f"LLM_MODEL={answers.llm_model}",
        f"{answers.llm_key_var}={answers.llm_key}",
        "",
        "# --- Голосовые сообщения ---",
        f"STT_ENABLED={'true' if answers.stt_enabled else 'false'}",
    ]

    if answers.stt_enabled:
        lines += [
            f"STT_PROVIDER={answers.stt_provider}",
            f"STT_MODEL={answers.stt_model}",
        ]
        if answers.stt_key:
            lines.append(f"{answers.stt_key_var}={answers.stt_key}")

    lines += [
        "",
        "# --- Supabase ---",
    ]
    if answers.supabase_url:
        lines += [
            f"SUPABASE_URL={answers.supabase_url}",
            f"SUPABASE_KEY={answers.supabase_key}",
        ]
    else:
        lines += [
            "# Не настроена: результаты не сохраняются, кнопок под ними нет.",
            "# SUPABASE_URL=https://your-project.supabase.co",
            "# SUPABASE_KEY=your-service-role-key",
        ]

    lines += [
        "",
        "# --- SearXNG (внутри того же контейнера) ---",
        "SEARXNG_URL=http://127.0.0.1:8888",
        "",
    ]
    return "\n".join(lines)


def write(body: str, path: Path) -> None:
    """Write *path* with owner-only permissions."""
    path.write_text(body, encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        # Windows filesystems may not support this; the file is still local.
        say("  (не удалось выставить права 0600 — файловая система не поддерживает)")


def summarise(answers: Answers, path: Path) -> None:
    say()
    say("=" * 68)
    say(f"  Записано: {path}")
    say("=" * 68)
    say(f"  Токен Telegram   {mask(answers.telegram_token)}")
    say(f"  Провайдер LLM    {answers.llm_provider} / {answers.llm_model}")
    say(f"  Ключ LLM         {mask(answers.llm_key)}")
    say(f"  Распознавание    {'включено: ' + answers.stt_provider if answers.stt_enabled else 'выключено'}")
    say(f"  Supabase         {answers.supabase_url or 'не подключена'}")
    say()
    say("  Запуск:  docker compose up --build")
    say("  Дождитесь строки startup.ready и напишите боту /start.")
    say()


def main() -> int:
    if ENV_PATH.exists():
        say(f"Файл {ENV_PATH} уже существует.")
        if not ask_yes_no("Перезаписать?", default=False):
            say("Отменено, ничего не изменено.")
            return 1

    try:
        answers = collect()
    except KeyboardInterrupt:
        say()
        say("Отменено, ничего не записано.")
        return 130

    write(render(answers), ENV_PATH)
    summarise(answers, ENV_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
