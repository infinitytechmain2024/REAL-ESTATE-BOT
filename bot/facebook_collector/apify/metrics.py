"""The Apify adapter's metrics as structured log events (the project has no Prometheus).

Each event is one INFO line of logger ``apify.metrics``: ``metric <name>=<value> <labels>``, with the same values in
the record's ``extra`` (``metric``, ``value`` and the labels) for a log shipper. The labels are correlation ids and
codes only -- ``campaign_id``, ``batch_id``, ``run_id``, ``actor_id``, ``source_url`` (a public group URL), ``reason``
-- never a token, a cookie or post text. Money and errors are also in ``campaign_costs`` (bot/utils/costs.py), which
the final report reads.
"""

from __future__ import annotations

import logging

log = logging.getLogger("apify.metrics")

METRICS = frozenset({
    "apify_run_launched_total", "apify_run_failed_total", "apify_items_imported_total", "apify_items_rejected_total",
    "apify_billed_usd_total", "apify_import_duration_seconds", "apify_checkpoint_resumed_total",
})
LABELS = frozenset({"campaign_id", "batch_id", "run_id", "actor_id", "source_url", "reason", "estimate"})


def emit(name: str, value: float = 1, **labels: object) -> None:
    """Log one metric event; an unknown metric or label is a programming error."""
    if name not in METRICS or set(labels) - LABELS:
        raise ValueError(f"unknown metric or label: {name} {sorted(set(labels) - LABELS)}")
    kept = {k: str(v)[:300] for k, v in labels.items() if v is not None}
    text = " ".join(f"{k}={v}" for k, v in sorted(kept.items()))
    log.info("metric %s=%s %s", name, round(float(value), 6), text, extra={"metric": name, "value": float(value), **kept})
