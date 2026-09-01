"""Prompt templates.

Kept in one place so the wording can be tuned without touching pipeline code.
All prompts instruct the model to answer in JSON; the schema itself is supplied
by :meth:`LLMProvider.chat_structured` from the Pydantic model, so these texts
only carry intent and domain rules.
"""

from bot.prompts.templates import (
    DETAILS_SYSTEM,
    EXTRACT_SYSTEM,
    RANK_SYSTEM,
    build_details_prompt,
    build_extract_prompt,
    build_rank_prompt,
)

__all__ = [
    "DETAILS_SYSTEM",
    "EXTRACT_SYSTEM",
    "RANK_SYSTEM",
    "build_details_prompt",
    "build_extract_prompt",
    "build_rank_prompt",
]
