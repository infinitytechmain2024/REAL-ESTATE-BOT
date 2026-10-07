"""SA-3 Recorder (hybrid pipeline phase 1): every finding is stored before it is sent, the stream is unchanged."""

from __future__ import annotations

import pytest

from bot.agents.recorder import (
    MemoryRecorder,
    PostgresRecorder,
    fingerprint,
    hamming,
    record_of,
    simhash64,
    site_of,
)
from bot.campaign.runner import CampaignRunner, RunnerConfig
from tests.test_near_match import (
    CHAT,
    GOAL,
    OWNER,
    USER,
    _seed_findings,
    add,
    cards,
    needs_db,
    pool,  # noqa: F401 - the PostgreSQL fixture
    press,
    setup,
)


async def with_recorder():
    campaigns, store, messenger, runner, cid, control = await setup()
    runner.recorder = MemoryRecorder()
    return campaigns, store, messenger, runner, cid, control


def row(runner: CampaignRunner, cid: str, fid: str):
    return runner.recorder.rows[(cid, fid)]


# --- what a row holds ---------------------------------------------------------------------


def test_site_url_key_fingerprint_and_simhash() -> None:
    assert site_of("https://m.facebook.com/groups/PisosMadrid/posts/1/") == "facebook.com/groups/pisosmadrid"
    assert site_of("https://www.idealista.com/inmueble/1/") == "idealista.com"
    payload = {"deal_type": "sale", "property_type": "land", "price_amount": 480_400, "price_currency": "eur",
               "area_m2": 2104, "location": "Boadilla del Monte, Madrid", "original_post_link": "https://www.fotocasa.es/x/1/d?utm_source=a"}
    assert fingerprint(payload) == "sale|land|480000|EUR|2100|boadilla-del-monte"
    assert fingerprint({"deal_type": "sale"}) is None
    record = record_of("c", "f", state="held", bucket="similar", payload=payload, text="Parcela en venta")
    assert (record.site, record.url_key) == ("fotocasa.es", record_of("c", "g", state="held", bucket=None,
            payload={"original_post_link": "https://fotocasa.es/x/1/d"}, text="").url_key)
    assert record.facts["area_m2"] == 2104 and "original_post_link" not in record.facts
    assert record_of("c", "f", state="held", bucket=None, payload=None, text="").url_key == "finding:f"

    text = "Vendo parcela urbanizable de 2.100 m2 en Boadilla del Monte, 480.000 euros, todos los servicios, llamar"
    repost = f"{text} +34 600 123 456 https://x.es/anuncio/1"  # phone and URL are ignored
    other = "Alquilo habitación luminosa en Lavapiés para estudiante, gastos incluidos, disponible en octubre ya"
    assert hamming(simhash64(text), simhash64(repost)) == 0
    assert hamming(simhash64(text), simhash64(other)) > 12
    assert simhash64("dos palabras") is None
    assert -(1 << 63) <= simhash64(text) < (1 << 63)  # fits a PostgreSQL bigint


# --- the stream with the recorder ------------------------------------------------------------


async def test_an_exact_card_is_stored_before_it_is_sent_then_marked_sent() -> None:
    _, store, messenger, runner, cid, _ = await with_recorder()
    order: list[str] = []
    to_send, send = runner.recorder.to_send, messenger.send

    async def spy_to_send(record):
        order.append("stored")
        await to_send(record)

    async def spy_send(chat, text, **kwargs):
        if "🔎" in text:
            order.append("sent")
        return await send(chat, text, **kwargs)

    runner.recorder.to_send, messenger.send = spy_to_send, spy_send
    add(store, cid, "f45", 45_000)
    await runner.tick()
    assert order == ["stored", "sent"]
    stored = row(runner, cid, "f45")
    assert (stored.state, stored.bucket, stored.site) == ("sent", "exact", "facebook.com/groups/pisos")
    assert stored.telegram_message_id == next(mid for _, mid, t in messenger.sent if "🔎" in t)
    assert stored.card_text == cards(messenger)[0]  # exactly what the user got


async def test_a_failed_send_stays_to_send_and_is_sent_once_later() -> None:
    _, store, messenger, runner, cid, _ = await with_recorder()
    add(store, cid, "f45", 45_000)
    messenger.fail = 1
    await runner.tick()
    assert cards(messenger) == []
    assert (row(runner, cid, "f45").state, row(runner, cid, "f45").send_attempts) == ("to_send", 1)
    await runner.tick()
    assert len(cards(messenger)) == 1
    assert (row(runner, cid, "f45").state, row(runner, cid, "f45").send_attempts) == ("sent", 1)
    await runner.tick()
    assert len(cards(messenger)) == 1


async def test_nothing_is_sent_while_the_store_is_down() -> None:
    _, store, messenger, runner, cid, _ = await with_recorder()
    add(store, cid, "f45", 45_000)
    runner.recorder.fail = True
    await runner.tick()
    assert cards(messenger) == [] and "f45" not in store.streamed.get(cid, set())
    await runner.tick()  # the store is back: stored, then sent, once
    assert len(cards(messenger)) == 1 and row(runner, cid, "f45").state == "sent"


async def test_held_and_excluded_findings_are_stored_and_an_approved_one_becomes_sent() -> None:
    _, store, messenger, runner, cid, control = await with_recorder()
    add(store, cid, "f60", 60_000)
    add(store, cid, "rent", 400, deal="rent")
    await runner.tick()
    assert (row(runner, cid, "f60").state, row(runner, cid, "f60").bucket) == ("held", "similar")
    assert (row(runner, cid, "rent").state, row(runner, cid, "rent").bucket) == ("excluded", "excluded")
    await press(control, USER, messenger.asks[0][2][0][1])  # «Одобрить»
    await runner.tick()
    assert (row(runner, cid, "f60").state, row(runner, cid, "f60").bucket) == ("sent", "similar")
    assert row(runner, cid, "rent").state == "excluded"


async def test_without_a_recorder_the_stream_is_unchanged() -> None:
    _, store, messenger, runner, cid, _ = await setup()
    add(store, cid, "f45", 45_000)
    await runner.tick()
    assert len(cards(messenger)) == 1 and runner.recorder is None


# --- PostgreSQL (migration 024) ----------------------------------------------------------------


@needs_db
async def test_postgres_rows_follow_the_stream(pool) -> None:  # noqa: F811
    from bot.campaign import plan_campaign
    from bot.campaign.runs import PostgresRunStore
    from bot.campaign.store import PostgresCampaignStore
    from bot.orchestra.store import SafetyLimits
    from tests.test_near_match import ButtonMessenger

    campaigns = PostgresCampaignStore(pool)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, PostgresRunStore(pool, SafetyLimits()), messenger, None, owner_ids={OWNER},
                            recorder=PostgresRecorder(pool), config=RunnerConfig(relevance_fail_closed=False))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=USER, source_text=GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    ids = await _seed_findings(pool, cid, {"f45": 45_000, "f60": 60_000})
    await runner.step(cid)
    rows = {r["finding_id"]: r for r in await pool.fetch(
        """select finding_id::text, state, bucket, site, url_key, fingerprint, simhash, facts, card_text,
                  telegram_message_id, sent_at from agent_findings where campaign_id = $1::uuid""", cid)}
    sent, held = rows[ids["f45"]], rows[ids["f60"]]
    assert (sent["state"], sent["bucket"], sent["site"]) == ("sent", "exact", "facebook.com/groups/pisos")
    assert sent["telegram_message_id"] == messenger.sent[0][1] and sent["sent_at"] is not None
    assert sent["card_text"] == cards(messenger)[0] and sent["fingerprint"].startswith("sale|apartment|45000|EUR")
    assert (held["state"], held["bucket"], held["telegram_message_id"]) == ("held", "similar", None)

    # A sent row never moves back, whatever is written later.
    recorder = PostgresRecorder(pool)
    await recorder.held(record_of(cid, ids["f45"], state="held", bucket="similar", payload=None, text=""))
    assert await pool.fetchval("select state from agent_findings where finding_id = $1::uuid", ids["f45"]) == "sent"
    with pytest.raises(Exception):  # noqa: B017 - the check constraint: sent needs sent_at
        await pool.execute("update agent_findings set sent_at = null where finding_id = $1::uuid", ids["f45"])
    # An excluded finding keeps why it was excluded (migration 039), e.g. a duplicate of a sent card.
    await recorder.excluded(record_of(cid, ids["f60"], state="excluded", bucket="excluded", payload=None, text="",
                                      reason=f"duplicate_of:{ids['f45']}"))
    assert await pool.fetchrow("select state, bucket, reason from agent_findings where finding_id = $1::uuid",
                               ids["f60"]) is not None
    row = await pool.fetchrow("select state, bucket, reason from agent_findings where finding_id = $1::uuid", ids["f60"])
    assert (row["state"], row["bucket"], row["reason"]) == ("excluded", "excluded", f"duplicate_of:{ids['f45']}")


def test_a_record_carries_its_reason() -> None:
    assert record_of("c", "f", state="excluded", bucket="excluded", payload=None, text="", reason="duplicate_of:f1").reason == "duplicate_of:f1"
    assert record_of("c", "f", state="held", bucket="similar", payload=None, text="").reason is None
