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

    def __init__(self) -> None:
        self.messages = None

    async def chat_structured(self, messages, schema, **kwargs):
        self.messages = messages
        # Simulate a small model guessing a boolean where the request says
        # explicitly that both variants are acceptable, and describing the
        # same criteria with inconsistent names and importance.
        return ParsedQuery.model_validate(
            {
                "mode": "land",
                "building_required": True,
                "location": {
                    "city": "Madrid",
                    "country": "Spain",
                    "raw": "пригороді Мадриду",
                },
                "criteria": [
                    {
                        "name": "suburbs of Madrid",
                        "value": "suburbs of Madrid",
                        "importance": "required",
                    },
                    {"name": "2000 m²", "value": "2000 m²", "importance": "required"},
                    {
                        "name": "for development",
                        "value": "for development",
                        "importance": "required",
                    },
                    {
                        "name": "5 minutes by car to metro",
                        "value": "5 minutes by car to metro",
                        "importance": "preferred",
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
    llm = EmptyStructuredLLM()
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

    async def chat_structured(self, messages, schema, **kwargs):
        return ParsedQuery(mode=Mode.LAND, location=self.location)


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

    assert parsed.location.city == city
    assert parsed.location.country == country
    assert parsed.location.raw == raw
    criteria = {criterion.name: criterion for criterion in parsed.criteria}
    assert criteria["location"].value == f"{city} suburbs"
    assert criteria["location"].importance == "required"
