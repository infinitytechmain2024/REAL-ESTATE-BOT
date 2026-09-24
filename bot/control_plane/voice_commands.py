"""Turn a short spoken phrase into a control-plane command.

Voice only ever maps to commands whose arguments can be said aloud: /status,
/help, and /pause|/resume|/cancel with the scope `all`. /run takes URLs, and
UUID scopes and confirmation tokens are random strings; those stay typed.
Mapping never skips confirmation -- a spoken "pause all" produces the same
confirmation prompt as the typed command.
"""

from __future__ import annotations

import re

# Longer utterances are conversation, not commands.
MAX_COMMAND_WORDS = 8

_STATUS = {"status", "статус", "статуса", "статусу", "стан", "estado"}
_HELP = {
    "help", "commands", "помощь", "помощи", "справка", "команды",
    "допомога", "допомогу", "довідка", "команди", "ayuda", "comandos",
}
_PAUSE = {
    "pause", "пауза", "паузу", "паузе", "приостанови", "приостановить",
    "паузі", "призупини", "призупинити", "pausa", "pausar", "pausalo",
}
_RESUME = {
    "resume", "continue", "продолжи", "продолжить", "возобнови", "возобновить",
    "продовж", "продовжи", "продовжити", "віднови", "відновити",
    "reanuda", "reanudar", "continua", "continuar",
}
_CANCEL = {
    "cancel", "отмена", "отмени", "отменить", "скасуй", "скасувати", "скасування",
    "cancela", "cancelar",
}
_ALL = {"all", "everything", "все", "всі", "усі", "усе", "todo", "todos", "toda", "todas"}

_LIFECYCLE = {"pause": _PAUSE, "resume": _RESUME, "cancel": _CANCEL}

# Whisper's well-known inventions on silence at the start or end of a clip:
# caption credits and sign-offs from its training data. Compared after
# normalisation, so punctuation and case do not matter.
_HALLUCINATIONS = {
    "thank you", "thanks", "thank you very much", "thank you for watching",
    "thanks for watching", "please subscribe", "bye",
    "subtitles by the amara org community",
    "спасибо", "спасибо за внимание", "спасибо за просмотр", "продолжение следует",
    "субтитры сделал dimatorzok", "субтитры создавал dimatorzok",
    "редактор субтитров а семкин корректор а егорова",
    "дякую", "дякую за перегляд", "дякую за увагу",
    "gracias", "gracias por ver", "gracias por ver el video",
    "subtitulos realizados por la comunidad de amara org",
}

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
# Only Spanish accents are folded: NFKD would also split Cyrillic й and ї.
_FOLD = str.maketrans("áéíóúüñё", "aeiouunе")


def _normalise(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", text.lower().translate(_FOLD)).split())


def clean_transcript(text: str) -> str:
    """Drop known Whisper sign-offs at either end, if anything else was said."""
    sentences = [part for part in _SENTENCE_END.split(text.strip()) if part.strip()]
    while len(sentences) > 1 and _normalise(sentences[0]) in _HALLUCINATIONS:
        sentences.pop(0)
    while len(sentences) > 1 and _normalise(sentences[-1]) in _HALLUCINATIONS:
        sentences.pop()
    return " ".join(sentence.strip() for sentence in sentences).strip("\"'«»“” ")


def spoken_command(text: str) -> str | None:
    """The command a short phrase asks for, or None when it is not clearly one."""
    stripped = text.strip()
    if stripped.startswith("/") or stripped.lower().startswith("confirm "):
        return None  # already a command; handled as typed text
    words = _normalise(stripped).split()
    if not words or len(words) > MAX_COMMAND_WORDS:
        return None
    found = set(words)
    matches: list[str] = []
    if found & _STATUS:
        matches.append("/status")
    if found & _HELP:
        matches.append("/help")
    for command, vocabulary in _LIFECYCLE.items():
        if found & vocabulary:
            # Only the scope `all` can be spoken; without it, nothing is mapped.
            matches.append(f"/{command} all" if found & _ALL else "")
    # Two different intents in one phrase ("cancel the pause") are ambiguous.
    if len(matches) != 1:
        return None
    return matches[0] or None
