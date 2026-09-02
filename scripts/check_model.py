#!/usr/bin/env python3
"""Find a model that can actually do the ranking call.

Picking a model by name is guesswork: catalogues change, and a model that
answers a one-line question in a second can still fail the ranking prompt,
which asks for a long structured answer over several pages of text. That is
exactly how a bot ends up silently serving search-engine snippets.

This measures it instead. It sends the same request shape the bot sends --
same endpoint, same payload keys, same JSON mode, a prompt built to the same
size from your own settings -- and reports how long each candidate took and
whether the answer parsed.

    python3 scripts/check_model.py --list
    python3 scripts/check_model.py
    python3 scripts/check_model.py meta/llama-3.3-70b-instruct another/model-id

Reads .env for the provider, key and base URL. Standard library only: no venv,
no container, no dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# Endpoint per provider name, mirroring bot/services/llm/.
BASE_URLS: dict[str, str] = {
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "nim": "https://integrate.api.nvidia.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "openai": "https://api.openai.com/v1",
    "openai_compatible": "https://api.openai.com/v1",
}

# Key variables per provider, in the order the bot consults them.
KEY_VARS: dict[str, tuple[str, ...]] = {
    "nvidia": ("LLM_API_KEY", "NVIDIA_API_KEY", "NGC_API_KEY"),
    "nim": ("LLM_API_KEY", "NVIDIA_API_KEY", "NGC_API_KEY"),
    "openrouter": ("LLM_API_KEY", "OPENROUTER_API_KEY"),
    "groq": ("LLM_API_KEY", "GROQ_API_KEY"),
    "together": ("LLM_API_KEY", "TOGETHER_API_KEY"),
    "fireworks": ("LLM_API_KEY", "FIREWORKS_API_KEY"),
    "openai": ("LLM_API_KEY", "OPENAI_API_KEY"),
    "openai_compatible": ("LLM_API_KEY", "OPENAI_API_KEY"),
}

VERDICT_OK = "ГОДИТСЯ"
VERDICT_SLOW = "МЕДЛЕННО"
VERDICT_BAD_JSON = "ЛОМАЕТ JSON"
VERDICT_FAIL = "НЕ РАБОТАЕТ"


def load_env(path: Path) -> dict[str, str]:
    """Minimal .env reader -- no dependency on python-dotenv."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve(env: dict[str, str], name: str, default: str = "") -> str:
    """Real environment wins over .env, matching how the bot loads settings."""
    return os.environ.get(name) or env.get(name) or default


def post(url: str, key: str, payload: dict[str, object], timeout: float) -> tuple[int, str]:
    """POST JSON, returning (status, body). Never raises on an HTTP error."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, f"{type(exc.reason).__name__ if exc.reason else 'URLError'}: {exc.reason}"
    except TimeoutError:
        return 0, "timeout"


def list_models(base_url: str, key: str) -> int:
    """Print the model ids this account can actually use."""
    request = urllib.request.Request(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic tool
        print(f"Не удалось получить список моделей: {exc}", file=sys.stderr)
        return 1

    ids = sorted(str(item.get("id", "")) for item in payload.get("data", []) if item.get("id"))
    if not ids:
        print("Список пуст — проверьте ключ и базовый URL.", file=sys.stderr)
        return 1

    print(f"Доступно моделей: {len(ids)}\n")
    # Instruct models are what the ranking call needs; surface them first.
    instruct = [i for i in ids if "instruct" in i.lower() or "-it" in i.lower()]
    other = [i for i in ids if i not in instruct]
    if instruct:
        print("--- инструкт-модели (то, что нужно для ранжирования) ---")
        for model_id in instruct:
            print(f"  {model_id}")
    if other:
        print("\n--- остальные ---")
        for model_id in other:
            print(f"  {model_id}")
    return 0


def build_probe(chars: int) -> list[dict[str, str]]:
    """A request shaped like the real ranking call, sized from your settings.

    The point is not realism of content but of *load*: the same order of
    magnitude of input, and an answer that has to be structured JSON of
    several objects. That is what separates a model that works here from one
    that answers short questions quickly and then times out in production.
    """
    filler = (
        "Parcela urbanizable de 2.480 m² en el término municipal de Boadilla del Monte, "
        "Madrid. Uso residencial, edificabilidad 0,4 m²/m². Servicios de agua, luz y "
        "alcantarillado en linde. Acceso asfaltado, a 6 minutos en coche de la estación. "
        "Precio 395.000 €. Referencia catastral disponible. Contacto: +34 600 123 456. "
    )
    pages = []
    for index in range(1, 6):
        text = (filler * 40)[:chars]
        pages.append(
            f"### Candidate {index}\n"
            f"URL: https://example-agency.es/parcela-{index}\n"
            f"Catalogue page: no\n"
            f"Extracted text:\n{text}\n"
        )

    return [
        {
            "role": "system",
            "content": (
                "You filter and summarise search results for a real-estate researcher. "
                "Score each candidate 0-100 on everything except price. Reply with a "
                "single JSON object of the form "
                '{"results": [{"url": "...", "title": "...", "summary": "...", '
                '"score": 0, "price_value": null, "price_currency": null}]} '
                "and nothing else."
            ),
        },
        {
            "role": "user",
            "content": (
                "Request: building plots from 2000 m2 near Madrid, Spain.\n\n"
                + "\n".join(pages)
                + "\nReturn all 5 candidates, best first."
            ),
        },
    ]


def probe(base_url: str, key: str, model: str, *, chars: int, max_tokens: int, timeout: float) -> None:
    """Run one candidate and print a verdict line."""
    payload = {
        "model": model,
        "messages": build_probe(chars),
        "temperature": 0.2,
        "max_tokens": max_tokens,
        # Exactly what bot/services/llm/openai_compatible.py sends.
        "response_format": {"type": "json_object"},
    }

    print(f"  {model}")
    started = time.monotonic()
    status, body = post(f"{base_url}/chat/completions", key, payload, timeout + 15)
    elapsed = time.monotonic() - started

    if status != 200:
        detail = body.strip().replace("\n", " ")[:160]
        print(f"    {VERDICT_FAIL:<12} {elapsed:6.1f}с  HTTP {status or '-'}  {detail}")
        return

    try:
        parsed = json.loads(body)
        content = parsed["choices"][0]["message"]["content"] or ""
        usage = parsed.get("usage") or {}
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        print(f"    {VERDICT_FAIL:<12} {elapsed:6.1f}с  ответ не разобрать: {exc}")
        return

    try:
        results = json.loads(content).get("results")
        json_ok = isinstance(results, list) and len(results) > 0
    except (ValueError, AttributeError):
        json_ok = False

    tokens = usage.get("completion_tokens", 0)
    stats = f"{elapsed:6.1f}с  вход {usage.get('prompt_tokens', 0)} / выход {tokens} токенов"

    if not json_ok:
        print(f"    {VERDICT_BAD_JSON:<12} {stats}  -- ответ не является ожидаемым JSON")
        print(f"    {'':<12} начало ответа: {content.strip()[:110]!r}")
        return
    if elapsed > timeout:
        print(f"    {VERDICT_SLOW:<12} {stats}  -- дольше LLM_TIMEOUT_SECONDS={timeout:g}")
        return
    print(f"    {VERDICT_OK:<12} {stats}")


def main(argv: list[str]) -> int:
    env = load_env(ENV_PATH)
    provider = resolve(env, "LLM_PROVIDER", "nvidia").lower()
    base_url = resolve(env, "LLM_BASE_URL") or BASE_URLS.get(provider, "")
    if not base_url:
        print(f"Неизвестный провайдер {provider!r}: задайте LLM_BASE_URL.", file=sys.stderr)
        return 2

    key = ""
    for var in KEY_VARS.get(provider, ("LLM_API_KEY",)):
        key = resolve(env, var)
        if key:
            break
    if not key:
        wanted = " / ".join(KEY_VARS.get(provider, ("LLM_API_KEY",)))
        print(f"Не найден ключ. Ожидается одна из переменных: {wanted}", file=sys.stderr)
        return 2

    if "--list" in argv:
        print(f"Провайдер: {provider}  |  {base_url}\n")
        return list_models(base_url, key)

    models = [arg for arg in argv if not arg.startswith("-")]
    if not models:
        configured = resolve(env, "LLM_MODEL")
        rank_model = resolve(env, "LLM_MODEL_RANK")
        models = [m for m in dict.fromkeys([rank_model, configured]) if m]
    if not models:
        print("Нечего проверять: укажите модель аргументом или задайте LLM_MODEL.", file=sys.stderr)
        return 2

    chars = int(resolve(env, "PARSER_MAX_CHARS", "3000"))
    max_tokens = int(resolve(env, "LLM_MAX_TOKENS", "2500"))
    timeout = float(resolve(env, "LLM_TIMEOUT_SECONDS", "180"))

    print(f"Провайдер: {provider}  |  {base_url}")
    print(f"Нагрузка как при ранжировании: 5 страниц по {chars} символов, "
          f"потолок ответа {max_tokens} токенов, лимит {timeout:g}с\n")

    for model in models:
        probe(base_url, key, model, chars=chars, max_tokens=max_tokens, timeout=timeout)

    print()
    print(f"{VERDICT_OK}     — ставьте её в LLM_MODEL (или в LLM_MODEL_RANK).")
    print(f"{VERDICT_SLOW}    — поднимите LLM_TIMEOUT_SECONDS или уменьшите")
    print("               PIPELINE_MAX_RANK_CANDIDATES и PARSER_MAX_CHARS.")
    print(f"{VERDICT_BAD_JSON}  — модель не держит JSON-режим, берите инструкт-модель.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
