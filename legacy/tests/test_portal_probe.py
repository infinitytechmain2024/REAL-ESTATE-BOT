"""The portal probe's judgement, checked against pages that really are served.

The point of the script is to distinguish three outcomes a deploy cares
about: a page we cannot fetch, a page we fetch but that extracts to nothing
useful, and a page worth ranking. Getting a 200 back is not the same as
having a listing, so the test serves each shape for real rather than
asserting on a constant.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import portal_probe

#: Sized like the real thing. A Spanish listing page extracts to well over a
#: thousand characters of description, so a fixture of two sentences would be
#: testing the threshold against something no portal actually serves.
LISTING = """
<html><body><h1>Piso en venta en Madrid</h1>
<p>350.000 €</p><p>120 m²</p><p>3 habitaciones, 2 baños</p>
<p>Bonito piso exterior reformado en el centro de Madrid, con ascensor y
garaje incluido. Muy luminoso, orientación sur, listo para entrar a vivir.
Zona tranquila y bien comunicada, a cinco minutos del metro y rodeada de
todos los servicios: colegios, supermercados, centros de salud y zonas
verdes. La vivienda se distribuye en un amplio salón comedor con salida a
balcón, cocina independiente totalmente equipada con electrodomésticos de
alta gama, tres dormitorios (el principal con vestidor y baño en suite) y
un segundo baño completo con ventana.</p>
<p>El edificio, construido en 1975 y rehabilitado integralmente en 2019,
cuenta con portero físico, ascensor adaptado y calefacción central de gas
natural. La comunidad es reducida y los gastos son moderados. Se incluye
en el precio una plaza de garaje en el mismo edificio y un trastero de
ocho metros cuadrados en la planta sótano.</p>
<p>Posibilidad de financiación hasta el 80% del valor de tasación. Consulte
disponibilidad para visitas; concertamos citas de lunes a sábado.</p>
</body></html>
"""

BANNER = "<html><body><p>Acepta las cookies para continuar</p></body></html>"


@pytest.fixture
async def site() -> AsyncIterator[str]:
    """Serves a real listing, a thin banner, and a 403."""

    async def handler(request: web.Request) -> web.Response:
        if request.path == "/listing":
            return web.Response(text=LISTING, content_type="text/html")
        if request.path == "/banner":
            return web.Response(text=BANNER, content_type="text/html")
        return web.Response(status=403, text="denied", content_type="text/html")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    yield f"http://127.0.0.1:{server.port}"
    await server.close()


async def test_a_real_listing_is_usable(site) -> None:
    [verdict] = await portal_probe.probe([f"{site}/listing"])

    assert verdict.summary == "USABLE"
    assert set(verdict.signals) >= {"price", "area"}
    assert verdict.usable


async def test_a_cookie_banner_is_not_a_listing(site) -> None:
    """A 200 that extracts to nothing must not count as a working portal."""
    [verdict] = await portal_probe.probe([f"{site}/banner"])

    assert verdict.page.ok, "the page itself was served fine"
    assert verdict.summary == "THIN"
    assert not verdict.usable


async def test_bot_protection_is_reported_as_blocked(site) -> None:
    [verdict] = await portal_probe.probe([f"{site}/denied"])

    assert verdict.summary == "BLOCKED"
    assert verdict.page.status == 403


async def test_the_report_recommends_the_browser_domains(site, capsys) -> None:
    """The bridge to 5.2: the probe names what to put in the setting."""
    verdicts = await portal_probe.probe([f"{site}/denied", f"{site}/listing"])
    exit_code = portal_probe.report(verdicts)

    output = capsys.readouterr().out
    assert "PARSER_BROWSER_DOMAINS=" in output
    assert exit_code == 1, "a blocked portal must fail the run"


async def test_all_usable_exits_zero(site, capsys) -> None:
    exit_code = portal_probe.report(await portal_probe.probe([f"{site}/listing"]))

    assert exit_code == 0
    assert "1/1 usable" in capsys.readouterr().out
