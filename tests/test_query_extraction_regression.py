from bot.config import Settings
from bot.models.enums import Mode
from bot.models.query import ParsedQuery
from bot.services.pipeline import ResearchPipeline

REQUEST = (
    "надай мені земельні участки від 2000 м квадратних з будинками або без, "
    "в пригороді Мадриду, близкість до метро в 5 хв на машині, "
    "участок має бути для забудови."
)


class EmptyStructuredLLM:
    """Model stub that exposes deterministic recovery from a sparse response."""

    def __init__(self) -> None:
        self.messages = None

    async def chat_structured(self, messages, schema, **kwargs):
        self.messages = messages
        # Simulate a small model guessing a boolean where the request says
        # explicitly that both variants are acceptable.
        return ParsedQuery(mode=Mode.LAND, building_required=True)


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
