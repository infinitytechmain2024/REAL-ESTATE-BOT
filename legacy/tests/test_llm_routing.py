from bot.config import LLMSettings


def test_model_pool_falls_back_to_legacy_split_settings() -> None:
    settings = LLMSettings(
        provider="openai_compatible",
        model="qwen3:8b",
        model_extract="qwen3:4b",
        model_rank="qwen3:14b",
    )

    assert settings.fast_model == "qwen3:4b"
    assert settings.strong_model == "qwen3:14b"
    assert settings.long_model == "qwen3:14b"


def test_model_router_assigns_pool_by_pipeline_task() -> None:
    settings = LLMSettings(
        provider="openai_compatible",
        model="qwen3:4b",
        model_fast="qwen3:8b",
        model_strong="qwen3:14b",
        model_long="mistral-small3.2:24b",
    )

    assert settings.model_for("extract") == "qwen3:8b"
    assert settings.model_for("rank") == "qwen3:14b"
    assert settings.model_for("details", content_chars=1000) == "qwen3:14b"
    assert settings.model_for("details", content_chars=7000) == "mistral-small3.2:24b"
