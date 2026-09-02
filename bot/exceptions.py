"""Exception hierarchy.

Every subsystem raises its own subclass so the pipeline can decide what to
degrade and what to surface. :attr:`BotError.user_message` is the text the user
is allowed to see -- it must never leak provider names, URLs or keys.
"""

from __future__ import annotations


class BotError(Exception):
    """Base class for every error this application raises deliberately."""

    default_user_message = "Что-то пошло не так. Попробуйте ещё раз чуть позже."

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or self.default_user_message


class ConfigurationError(BotError):
    """The deployment is misconfigured; usually fatal at start-up."""

    default_user_message = "Бот сейчас настроен неполностью. Сообщите администратору."


class LLMError(BotError):
    """An LLM provider failed. Triggers the fallback chain."""

    default_user_message = "Не удалось обработать запрос через ИИ. Попробуйте ещё раз."


class LLMResponseError(LLMError):
    """The provider answered, but not in the shape we asked for."""


class LLMTimeoutError(LLMError):
    """The provider did not answer in time.

    Worth its own class because retrying is far more expensive here than for
    other failures: each attempt burns the whole timeout before failing. A
    request that was too big or a model that is too slow will time out again,
    so the manager retries this at most once before moving on.
    """


class STTError(BotError):
    """Transcription failed."""

    default_user_message = "Не получилось распознать голосовое сообщение. Напишите текстом, пожалуйста."


class SearchError(BotError):
    """SearXNG is unreachable or answered with an error."""

    default_user_message = "Поисковый сервис сейчас недоступен. Попробуйте через минуту."


class FetchError(BotError):
    """A single page could not be downloaded. Never fatal."""

    default_user_message = "Не удалось открыть страницу."


class StorageError(BotError):
    """Supabase rejected an operation."""

    default_user_message = "Не удалось сохранить результат, но поиск отработал."


class RateLimitedError(BotError):
    """The user is sending requests faster than the cooldown allows."""

    default_user_message = "Слишком часто. Подождите пару секунд и повторите."
