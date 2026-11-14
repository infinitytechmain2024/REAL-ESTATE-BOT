"""Q46: the tenant rule of Q45 made precise (REVIEW_TENANT_HITS, the same flag, off by default): «ocupada» means
people, a label per kind of sign, the advert «¡Puja por…!», a usufruct of common areas and a rent-to-own are no sign;
the tenant hits come first and a negated word takes no window. Mocks only, no network.
"""

from __future__ import annotations

import itertools

import pytest

from bot.agents import reviewer as rv
from bot.campaign import quote_hits as quote_hits_module
from bot.campaign.quote_hits import HITS_PER_TERM, MAX_HITS, quote_hits, tenant_terms
from tests.test_q45_tenant_hits import FLAT_SPEC, OLD_SHA256, _run, sha

FLAGS = list(itertools.product((False, True), repeat=4))


def _rule() -> str:
    system = rv.prompt_of(tenant=True)[1]
    return system.split("- Not free to move in:", 1)[1].split("- budget, rooms, area:", 1)[0]


@pytest.mark.parametrize("flags", FLAGS)
def test_flag_off_prompts_are_still_byte_for_byte_the_old_ones(flags) -> None:
    soft, th, hits, nr = flags
    version, system = rv.prompt_of(soft_wishes=soft, townhouse_is_house=th, quote_hits=hits, non_residential=nr)
    assert sha(system) == OLD_SHA256[flags] and "-tn" not in version


def test_a_label_per_kind_of_sign() -> None:
    rule = _rule()
    kinds = {
        "«с арендатором»": ("«alquilada»", "«arrendada»", "«inquilinos»"),
        "«занята»": ("«ocupada»", "«okupas»", "occupied by people"),
        "«без права проживания»": ("«nuda propiedad»", "«usufructo»"),
        "«аукцион»": ("«subasta»", "«pujas»"),
        "«аренда с выкупом»": ("«alquiler con opción a compra»",),
    }
    for label, words in kinds.items():
        bullet = rule.split(f"* {label}:", 1)[1].split("\n  * ", 1)[0]
        for word in words:
            assert word in bullet, (label, word)


def test_no_sign_cases() -> None:
    no_sign = _rule().split("No sign:", 1)[1]
    for word in ("«superficie\n  ocupada»", "«ocupada por un trastero»", "«ocupada por la terraza»",
                 "«usufructo de zonas comunes»", "«¡Puja por tu nuevo hogar!»", "«alquiler con opción a compra»",
                 "never «с арендатором»", "«libre de inquilinos»", "«ideal para alquilar»"):
        assert word in no_sign, word


def test_a_negated_tenant_word_takes_no_window() -> None:
    assert quote_hits("Se entrega sin inquilinos, libre de ocupantes. No alquilada.", tenant_terms()) == []
    assert quote_hits("Nunca alquilado, libres de okupas.", tenant_terms()) == []
    filler = " palabra" * 40
    text = f"Vivienda sin inquilinos.{filler} Garaje no alquilado.{filler} Hoy actualmente alquilada a un inquilino."
    hits = quote_hits(text, tenant_terms())
    assert len(hits) == 1 and "actualmente alquilada" in hits[0]  # the negated ones never use up the quota
    assert quote_hits("Chalet sin piscina.", ["бассейн"]) == ["Chalet sin piscina."]  # a task word: as before


def test_vivienda_sin_posesion_is_still_a_hit() -> None:
    assert quote_hits("Vivienda sin posesión, se vende tal cual.", tenant_terms())


def test_tenant_terms_first_keep_their_window_among_many_task_words() -> None:
    words = {"сад": "jardín", "бассейн": "piscina", "терраса": "terraza", "лифт": "ascensor", "гараж": "garaje",
             "балкон": "balcón", "камин": "chimenea", "кладовая": "trastero"}
    filler = " palabra" * 40
    text = "".join(f"Con {w} uno.{filler} Con {w} dos.{filler}" for w in words.values()) + " Actualmente alquilada."
    late = quote_hits(text, [*words, *tenant_terms()])
    first = quote_hits(text, [*tenant_terms(), *words])
    assert len(late) == len(first) == MAX_HITS and len(words) * HITS_PER_TERM > MAX_HITS
    assert not any("alquilada" in h for h in late) and any("alquilada" in h for h in first)


async def test_the_runner_puts_the_tenant_terms_first(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_terms: list[list] = []
    real = quote_hits_module.quote_hits

    def spy(text, terms, **kwargs):
        seen_terms.append(list(terms))
        return real(text, terms, **kwargs)

    monkeypatch.setattr(quote_hits_module, "quote_hits", spy)
    store, _ = await _run(tenant=True, spec={**FLAT_SPEC, "must_have": ["сад", "лифт"]})
    assert seen_terms == [[*tenant_terms(), "сад", "лифт"]]
    assert store.buckets["flat"][0] == "similar"
