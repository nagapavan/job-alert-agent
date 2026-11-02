"""Per-channel discovery outcomes and run-level status aggregation.

A discovery run touches several independent channels (ATS portals, LinkedIn, Gmail,
Google Jobs, ...). Historically every run was recorded as ``success`` even when a
channel never executed — a disconnected Gmail API, a missing optional dependency, or a
fetcher that returned nothing for a reason nobody could see. The operator could not
tell "nothing to find" from "this channel is dead".

This module keeps the vocabulary in one place so runners and the UI agree on what a
channel outcome means.
"""

OK = "ok"  # ran and produced a real outcome (including a legitimate zero)
SKIPPED_FLAG = "skipped_flag"  # deliberately not requested / disabled by the operator
NOT_CONNECTED = "not_connected"  # credentials missing or revoked
DEPENDENCY_MISSING = "dependency_missing"  # optional package/service not available
DEGRADED_ZERO = "degraded_zero"  # ran but a known precondition failed, so 0 is meaningless
ERROR = "error"  # raised

# Outcomes that mean "this channel did what it was asked". Everything else is a
# degradation the operator should be told about.
HEALTHY = frozenset({OK, SKIPPED_FLAG})

MAX_REASON_CHARS = 240


def record(outcomes, channel, outcome, reason=""):
    """Appends a normalized outcome dict. ``outcomes`` may be None (no-op)."""
    if outcomes is None:
        return
    text = (reason or "").strip()
    if len(text) > MAX_REASON_CHARS:
        text = text[: MAX_REASON_CHARS - 1].rstrip() + "…"
    outcomes.append({"channel": channel, "outcome": outcome, "reason": text})


def unhealthy(outcomes):
    """Outcomes that represent a degradation (skipped channels are not degradations)."""
    return [o for o in (outcomes or []) if o.get("outcome") not in HEALTHY]


def aggregate_status(outcomes):
    """Run-level status: ``success`` only when nothing degraded.

    - no channels requested at all -> ``success`` (nothing was asked for)
    - every requested channel raised -> ``Failed``
    - any other degradation (not connected, missing dependency, meaningless zero) -> ``Partial``

    ``Failed`` is reserved for actual exceptions: a channel that ran but found nothing
    for a known reason is degraded, not failed.
    """
    requested = [o for o in (outcomes or []) if o.get("outcome") != SKIPPED_FLAG]
    if not requested:
        return "success"
    bad = unhealthy(outcomes)
    if not bad:
        return "success"
    if all(o.get("outcome") == ERROR for o in requested):
        return "Failed"
    return "Partial"


def describe(outcomes):
    """Human-readable one-liner listing degraded channels, for summaries/details."""
    bad = unhealthy(outcomes)
    if not bad:
        return ""
    parts = []
    for o in bad:
        reason = f" ({o['reason']})" if o.get("reason") else ""
        parts.append(f"{o['channel']}: {o['outcome']}{reason}")
    return "; ".join(parts)
