"""Default-off Idealista adapter; fixtures reflect documentation, not a live validation.

The actor's ambiguous ``size`` is deliberately omitted for land. Description facts
remain available to the ordinary analysis/verification pipeline.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from bot.utils import costs
from bot.utils.listing_text import plot_area
from bot.web_search.geocoding import CachedCityCenters
from bot.web_search.queries import QueryTask, task_kind
from bot.web_search.sources.base import SourceListing
from bot.web_search.urls import fetchable, host_of


class ApifyError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TemporaryApifyError(ApifyError):
    """Resume a known run later; never repeat an uncertain launch."""


class PermanentApifyError(ApifyError):
    """This source attempt cannot produce listings; ordinary search continues."""


@dataclass(frozen=True, slots=True)
class SourceRunContext:
    campaign_id: str
    source_name: str
    run_id: str | None
    dataset_id: str | None
    max_charge_usd: float
    launch_allowed: bool
    save_run: Callable[[str, str | None], Awaitable[None]]
    reconcile_usage: Callable[..., Awaitable[None]]


_execution: ContextVar[SourceRunContext | None] = ContextVar("apify_execution", default=None)


@contextmanager
def source_run_scope(context: SourceRunContext) -> Iterator[None]:
    token = _execution.set(context)
    try:
        yield
    finally:
        _execution.reset(token)


class ApifyIdealistaSource:
    name = "apify_idealista"
    hosts = frozenset({"idealista.com"})

    def __init__(self, token: str, *, actor_id: str = "axlymxp/idealista-scraper",
                 dynamic_actor_id: str = "",
                 location_name: str = "Madrid", location_id: str = "", max_items: int = 50,
                 timeout_seconds: float = 120, max_charge_usd: float = .10,
                 client: httpx.AsyncClient | None = None,
                 geocoder: CachedCityCenters | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 max_response_bytes: int = 2_000_000) -> None:
        if not re.fullmatch(r"[\w-]+(?:[/~][\w-]+)?", actor_id):
            raise ValueError("invalid actor id")
        if dynamic_actor_id and not re.fullmatch(r"[\w-]+(?:[/~][\w-]+)?", dynamic_actor_id):
            raise ValueError("invalid dynamic actor id")
        if not 1 <= max_items <= 50 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("invalid apify limits")
        if not math.isfinite(max_charge_usd) or max_charge_usd <= 0:
            raise ValueError("invalid apify charge cap")
        self._token, self.actor_id = token, actor_id.replace("/", "~")
        self.dynamic_actor_id = dynamic_actor_id.replace("/", "~")
        self.location_name, self.location_id = location_name.strip(), location_id.strip()
        self.max_items, self.timeout_seconds, self.max_charge_usd = max_items, timeout_seconds, max_charge_usd
        self._sleep, self.max_response_bytes = sleep, max_response_bytes
        self._geocoder = geocoder
        self._client = client or httpx.AsyncClient(timeout=min(30, timeout_seconds), follow_redirects=False)

    def __repr__(self) -> str:
        return "ApifyIdealistaSource(...)"

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._geocoder is not None:
            await self._geocoder.aclose()

    async def settle(self, context: SourceRunContext) -> None:
        """Cleanup an existing paid run after cancellation/expiry, without fetching listings."""
        run_id = context.run_id or ""
        if not re.fullmatch(r"[A-Za-z0-9]+", run_id):
            raise PermanentApifyError("apify_run_schema")
        cap = min(self.max_charge_usd, context.max_charge_usd)
        try:
            run = await self._run("GET", f"actor-runs/{run_id}")
            if run.get("status") not in {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}:
                run = await self._run("POST", f"actor-runs/{run_id}/abort")
            await self._bill(context, run_id, run, cap)
        except ApifyError:
            await context.reconcile_usage(run_id, cap, estimated=True)
            raise

    def supports(self, task: QueryTask) -> bool:
        return bool(self._token and task.location.strip()
                    and task.country == "ES" and task.vertical == "real_estate"
                    and task.place_level == "city" and task_kind(task) == "land"
                    and task.constraints.get("deal") == "sale"
                    and (task.radius_km is None or (0 < task.radius_km <= 100
                         and self._geocoder is not None and self.dynamic_actor_id))
                    and (self._static_for(task) or self.dynamic_actor_id)
                    and not any(h == "idealista.com" or "idealista.com".endswith("." + h)
                                for h in task.blocked_hosts))

    def _static_for(self, task: QueryTask) -> bool:
        return bool(not task.suburbs_only and task.radius_km is None and self.location_id and self.location_name
                    and _fold(task.location) == _fold(self.location_name))

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._client.stream(
                    method, "https://api.apify.com/v2/" + path,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=min(30, self.timeout_seconds), follow_redirects=False, **kwargs) as response:
                if response.status_code >= 300:
                    cls = TemporaryApifyError if response.status_code == 429 or response.status_code >= 500 else PermanentApifyError
                    raise cls(f"apify_http_{response.status_code}")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > self.max_response_bytes:
                        raise PermanentApifyError("apify_response_too_large")
                    chunks.append(chunk)
        except httpx.HTTPError:
            raise TemporaryApifyError("apify_transport") from None
        try:
            return json.loads(b"".join(chunks))
        except ValueError:
            raise PermanentApifyError("apify_invalid_json") from None

    async def _run(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        body = await self._request(method, path, **kwargs)
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise PermanentApifyError("apify_run_schema")
        return body["data"]

    async def search(self, task: QueryTask, *, limit: int) -> list[SourceListing]:
        started = time.monotonic()
        if limit <= 0 or not self.supports(task):
            return []
        context = _execution.get()
        if context is None or context.source_name != self.name:
            raise PermanentApifyError("apify_claim_required")
        cap = min(self.max_charge_usd, context.max_charge_usd)
        if not math.isfinite(cap) or cap <= 0:
            raise PermanentApifyError("apify_budget_cap")
        run_id = context.run_id
        amount = min(limit, self.max_items)
        center: tuple[float, float] | None = None
        if self._static_for(task):
            actor = self.actor_id
            actor_input = {"country": "es", "locationName": self.location_name,
                           "locationId": self.location_id, "propertyType": "lands",
                           "operation": "sale", "maxItems": amount, "numPage": 1}
        else:
            actor = self.dynamic_actor_id
            # This actor resolves the user's place name through Idealista autocomplete.
            # Its published input schema requires maxProperties >= 10; importing is still
            # bounded by amount and the provider-side maximum charge.
            actor_input = {"country": "es", "searchType": "auto", "locationPrefix": task.location,
                           "propertyType": "lands", "operation": "sale",
                           "maxProperties": max(10, amount)}
            if task.radius_km is not None and not run_id:
                center = await self._geocoder.resolve(task.location) if self._geocoder else None
                if center is None:
                    raise PermanentApifyError("apify_geocode_unavailable")
                actor_input = {"country": "es", "searchType": "center",
                               "center": f"{center[0]:.6f},{center[1]:.6f}",
                               "distance": max(100, min(100_000, round(task.radius_km * 1000))),
                               "propertyType": "lands", "operation": "sale",
                               "maxProperties": max(10, amount)}
        if not run_id:
            if not context.launch_allowed:
                raise PermanentApifyError("apify_launch_uncertain")
            if await costs.over_budget(context.campaign_id):
                raise PermanentApifyError("apify_budget_cap")
            try:
                run = await self._run(
                    "POST", f"acts/{actor}/runs",
                    params={"maxTotalChargeUsd": cap, "maxItems": amount,
                            "timeout": self.timeout_seconds, "restartOnError": "false"},
                    json=actor_input)
                run_id = _text(run.get("id"))
                if not re.fullmatch(r"[A-Za-z0-9]+", run_id):
                    raise PermanentApifyError("apify_run_schema")
            except ApifyError as exc:
                # POST may have launched remotely: no second launch, reserve the cap explicitly.
                if not exc.code.startswith("apify_http_4"):
                    await context.reconcile_usage("", cap, estimated=True)
                raise
            try:
                await context.save_run(run_id, _text(run.get("defaultDatasetId")) or None)
            except Exception:  # noqa: BLE001 - checkpoint failures must abort the already paid run safely
                try:
                    run = await self._run("POST", f"actor-runs/{run_id}/abort")
                    await self._bill(context, run_id, run, cap)
                except Exception:  # noqa: BLE001 - cleanup/cost storage errors must not expose database details
                    with suppress(Exception):  # preserve the safe checkpoint code when cost storage also fails
                        await context.reconcile_usage(run_id, cap, estimated=True)
                raise PermanentApifyError("apify_checkpoint_failed") from None
        else:
            if not re.fullmatch(r"[A-Za-z0-9]+", run_id):
                raise PermanentApifyError("apify_run_schema")
            try:
                run = await self._run("GET", f"actor-runs/{run_id}")
            except ApifyError:
                await context.reconcile_usage(run_id, cap, estimated=True)
                raise
        # Cleanup/polling an already launched run is allowed even after campaign budget exhaustion.
        try:
            async with asyncio.timeout(max(0, self.timeout_seconds - (time.monotonic() - started))):
                while run.get("status") in {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}:
                    await asyncio.sleep(.1)
                    run = await self._run("GET", f"actor-runs/{run_id}",
                                          params={"waitForFinish": min(20, self.timeout_seconds)})
        except TimeoutError:
            # Abort prevents an unbounded remote run; next invocation reconciles the same ID.
            try:
                run = await self._run("POST", f"actor-runs/{run_id}/abort")
            except ApifyError:
                await context.reconcile_usage(run_id, cap, estimated=True)
                raise TemporaryApifyError("apify_wait_timeout") from None
            await self._bill(context, run_id, run, cap)
            raise TemporaryApifyError("apify_wait_timeout") from None
        except ApifyError:
            await context.reconcile_usage(run_id, cap, estimated=True)
            raise
        status = run.get("status")
        if status not in {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}:
            await context.reconcile_usage(run_id, cap, estimated=True)
            raise PermanentApifyError("apify_run_status")
        await self._bill(context, run_id, run, cap)
        if status != "SUCCEEDED":
            raise PermanentApifyError("apify_run_" + status.lower().replace("-", "_"))
        dataset_id = _text(run.get("defaultDatasetId")) or context.dataset_id
        if not dataset_id or not re.fullmatch(r"[A-Za-z0-9]+", dataset_id):
            raise PermanentApifyError("apify_dataset_schema")
        await context.save_run(run_id, dataset_id)
        if await costs.over_budget(context.campaign_id):
            raise PermanentApifyError("apify_budget_cap")
        try:
            async with asyncio.timeout(max(0, self.timeout_seconds - (time.monotonic() - started))):
                items = await self._request("GET", f"datasets/{dataset_id}/items",
                                            params={"format": "json", "clean": "true", "offset": 0, "limit": amount})
        except TimeoutError:
            raise TemporaryApifyError("apify_dataset_timeout") from None
        if not isinstance(items, list):
            raise PermanentApifyError("apify_dataset_schema")
        overflow = max(0, len(items) - amount)
        if overflow:
            await costs.record("api", kind="skip", provider="apify", item=actor,
                               code="apify_result_limit", units=overflow, campaign_id=context.campaign_id)
        if task.radius_km is not None and center is None:
            center = await self._geocoder.resolve(task.location) if self._geocoder else None
            if center is None:
                raise PermanentApifyError("apify_geocode_unavailable")
        rows = [normalize_listing(item, task, municipality=task.location, center=center) for item in items[:amount]]
        invalid = sum(row is None for row in rows)
        if invalid:
            await costs.record("api", kind="skip", provider="apify", item=actor,
                               code="apify_invalid_listing", units=invalid, campaign_id=context.campaign_id)
        return [row for row in rows if row is not None]

    async def _bill(self, context: SourceRunContext, run_id: str, run: dict[str, Any], cap: float) -> None:
        # The completion payload's cost may still be settling; authenticated re-read
        # follows Apify's recommendation before treating usage as final.
        await self._sleep(10)
        try:
            final = await self._run("GET", f"actor-runs/{run_id}")
        except ApifyError:
            await context.reconcile_usage(run_id, cap, estimated=True)
            raise TemporaryApifyError("apify_billing_pending") from None
        usage = _number(final.get("usageTotalUsd"))
        await context.reconcile_usage(run_id, usage if usage is not None else cap,
                                      estimated=usage is None)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(c)).strip()


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) and (result > 0 if positive else result >= 0) else None


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = (math.radians(x) for x in (*a, *b))
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    h = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _listing_point(item: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lat, lon = float(item.get("latitude")), float(item.get("longitude"))
    except (TypeError, ValueError, OverflowError):
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180 else None


def normalize_listing(item: Any, task: QueryTask, *, municipality: str,
                      center: tuple[float, float] | None = None) -> SourceListing | None:
    """Reject foreign/wrong-transaction rows; normalize only independently supplied facts."""
    if not isinstance(item, dict):
        return None
    url = _text(item.get("url"))
    if len(url) > 2048 or not fetchable(url, task.blocked_hosts) or host_of(url) != "idealista.com":
        return None
    parts = urlsplit(url)
    if parts.hostname not in {"idealista.com", "www.idealista.com"} or not re.fullmatch(
        r"/(?:en/|ca/)?inmueble/[0-9]+/?", parts.path
    ):
        return None
    found_municipality = _fold(_text(item.get("municipality")))
    if (_text(item.get("country")).casefold() != "es"
            or _text(item.get("operation")).casefold() != "sale"
            or _text(item.get("propertyType")).casefold() not in {"land", "lands"}):
        return None
    if task.radius_km is not None:
        point = _listing_point(item)
        if center is None or point is None or _distance_km(center, point) > task.radius_km:
            return None
    elif found_municipality not in {_fold(municipality), *(_fold(p) for p in task.nearby_places)}:
        return None
    if task.suburbs_only and found_municipality == _fold(municipality):
        return None
    price = _number(item.get("price"), positive=True)  # documented as EUR, never inferred from the request
    rooms = _number(item.get("rooms"), positive=True)
    address = ", ".join(filter(None, (_text(item.get("address")),
                                     _text(item.get("municipality")),
                                     _text(item.get("province"))))) or None
    description = _text(item.get("description"))[:1450 if task.radius_km is not None else 1500]
    # ``size`` alone is undocumented as plot versus built area. Accept a plot
    # only when the description labels one and the two independent values agree.
    reported_size = _number(item.get("size"), positive=True)
    described_plot = plot_area(description)
    confirmed_plot = (described_plot if reported_size is not None and described_plot is not None
                      and abs(described_plot - reported_size) <= reported_size * .10 else None)
    if task.radius_km is not None:
        description += "\nГеоданные: © OpenStreetMap contributors"
    return SourceListing(
        url=url, title=(_text(item.get("title")) or "Terreno en venta")[:300],
        price=price, currency="EUR" if price is not None else None,
        rooms=int(rooms) if rooms is not None and rooms.is_integer() and rooms <= 99 else None,
        address=address[:300] if address else None, property_type="landparcel", deal="sale",
        description=description, plot_m2=confirmed_plot,
    )
