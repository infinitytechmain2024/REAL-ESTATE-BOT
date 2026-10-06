import pytest

from bot.config import Settings
from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.services.pipeline import ResearchPipeline

REQUEST = (
    "надай мені земельні участки від 2000 м квадратних з будинками або без, "
    "в пригороді Мадриду, близкість до метро в 5 хв на машині, "
    "участок має бути для забудови."
)


def test_query_criteria_accept_common_local_model_json_variants():
    parsed = ParsedQuery.model_validate(
        {
            "mode": "land",
            "criteria": [
                {"name": "buildable", "value": True, "importance": "required"},
                {"type": "required", "value": "area ≥ 2000 m²"},
            ],
        }
    )

    assert parsed.criteria[0].value == "true"
    assert parsed.criteria[1].name == "area ≥ 2000 m²"
    assert parsed.criteria[1].importance == "required"


class EmptyStructuredLLM:
    """Model stub that exposes deterministic recovery from a sparse response."""

    def __init__(self, repair_payload: dict | None = None) -> None:
        self.messages = None
        self.calls = 0
        self.repair_payload = repair_payload or {
            "city": "Madrid",
            "country": "Spain",
            "raw": "пригороді Мадриду",
        }

    async def chat_structured(self, messages, schema, **kwargs):
        self.calls += 1
        if self.calls > 1:
            return schema.model_validate(self.repair_payload)
        self.messages = messages
        # Simulate a small model guessing a boolean where the request says
        # explicitly that both variants are acceptable, and describing the
        # same criteria with inconsistent names and importance.
        return ParsedQuery.model_validate(
            {
                "mode": "land",
                "building_required": True,
                "location": {"raw": "пригороді Мадриду"},
                "criteria": [
                    {"type": "required", "value": 2000},
                    {"type": "required", "value": True},
                    {
                        "name": "5 minutes",
                        "value": "5 minutes",
                        "importance": "preferred",
                    },
                    {
                        "name": "with or without",
                        "value": "with or without",
                        "importance": "optional",
                    },
                ],
            }
        )


async def test_extract_query_recovers_critical_criteria_from_exact_user_request():
    llm = EmptyStructuredLLM()
    pipeline = ResearchPipeline(
        settings=Settings(),
        llm=llm,
        search=None,
        query_builder=None,
        fetcher=None,
        repo=None,
    )

    parsed = await pipeline.extract_query(REQUEST, Mode.LAND)

    assert llm.calls == 2
    assert REQUEST in llm.messages[1].content
    assert parsed.area_min == 2000
    assert parsed.location.city == "Madrid"
    assert parsed.location.country == "Spain"
    assert parsed.metro_drive_minutes == 5
    assert parsed.buildable_required is True
    assert parsed.building_required is None
    assert parsed.object_type == "land plot"
    assert parsed.languages[:2] == ["es", "en"]
    assert len(parsed.languages) == len(set(parsed.languages))

    criteria = {criterion.name: criterion for criterion in parsed.criteria}
    assert len(criteria) == 5
    assert criteria["area_min"].value == "2000 m²"
    assert criteria["area_min"].importance == "required"
    assert criteria["location"].value == "Madrid suburbs"
    assert criteria["location"].importance == "required"
    assert criteria["metro_drive_minutes"].value == "5 minutes by car"
    assert criteria["metro_drive_minutes"].importance == "required"
    assert criteria["buildable_required"].importance == "required"
    assert criteria["building"].importance == "optional"


async def test_recovery_does_not_turn_walking_time_or_negative_use_into_requirements():
    llm = EmptyStructuredLLM(repair_payload={"raw": None})
    pipeline = ResearchPipeline(
        settings=Settings(),
        llm=llm,
        search=None,
        query_builder=None,
        fetcher=None,
        repo=None,
    )

    parsed = await pipeline.extract_query(
        "Участок в 5 минутах пешком от метро, не для застройки.", Mode.LAND
    )

    assert parsed.metro_drive_minutes is None
    assert parsed.buildable_required is False


class LocationStructuredLLM:
    def __init__(self, location: Location) -> None:
        self.location = location
        self.calls = []

    async def chat_structured(self, messages, schema, **kwargs):
        self.calls.append((messages, schema, kwargs))
        if len(self.calls) == 1:
            # The main extraction kept the user's phrase but failed to
            # normalise it -- the real qwen3:8b failure this test protects.
            return ParsedQuery(mode=Mode.LAND, location=Location(raw=self.location.raw))
        return schema.model_validate(self.location.model_dump())


@pytest.mark.parametrize(
    ("request_text", "city", "country", "raw"),
    [
        ("земельна ділянка в пригороді Валенсії", "Valencia", "Spain", "пригороді Валенсії"),
        ("земельна ділянка в пригороді Аліканте", "Alicante", "Spain", "пригороді Аліканте"),
        ("земельна ділянка в пригороді Ларнаки", "Larnaca", "Cyprus", "пригороді Ларнаки"),
    ],
)
async def test_location_repair_is_generic_and_preserves_any_model_place(
    request_text: str, city: str, country: str, raw: str,
):
    """Suburb criteria must use the model's place, not a Python city list."""
    pipeline = ResearchPipeline(
        settings=Settings(),
        llm=LocationStructuredLLM(Location(city=city, country=country, raw=raw)),
        search=None,
        query_builder=None,
        fetcher=None,
        repo=None,
    )

    parsed = await pipeline.extract_query(request_text, Mode.LAND)

    assert len(pipeline.llm.calls) == 2
    assert parsed.location.city == city
    assert parsed.location.country == country
    assert parsed.location.raw == raw
    criteria = {criterion.name: criterion for criterion in parsed.criteria}
    assert criteria["location"].value == f"{city} suburbs"
    assert criteria["location"].importance == "required"


class HallucinatingLocationLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_structured(self, messages, schema, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ParsedQuery(mode=Mode.LAND)
        return schema.model_validate({"city": "Madrid", "country": "Spain", "raw": "Madrid"})


async def test_location_repair_rejects_a_place_not_present_in_the_request():
    llm = HallucinatingLocationLLM()
    pipeline = ResearchPipeline(
        settings=Settings(),
        llm=llm,
        search=None,
        query_builder=None,
        fetcher=None,
        repo=None,
    )

    parsed = await pipeline.extract_query("land, at least 2 hectares", Mode.LAND)

    assert llm.calls == 2
    assert parsed.location.is_empty()
