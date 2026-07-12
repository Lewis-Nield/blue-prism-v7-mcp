"""Shared tool-layer plumbing: the envelope, loud validation, name→UUID
resolution, and the cached scrub boundary.

These are the contracts every tool carries (see DESIGN.md "Shared contract"):
list tools return a relevance-sorted, honestly-truncated envelope; dated
parameters fail loudly on malformed input; entity names resolve to UUIDs via
the (cached) list endpoints; and per-message PII scrubbing is cached because an
LLM client re-reads the same rows across calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from difflib import get_close_matches
from functools import lru_cache
from typing import Any, Callable
from uuid import UUID

import requests

from ..pii import Scrubber

# Default cap on list-tool results. A large estate (200+ processes, months of
# data) would otherwise blow the model's context and token budget on a single
# call. The list tools sort server-side by relevance first, then return the top
# N inside an envelope whose meta tells the model exactly how much it did NOT
# see — so it can raise the limit or narrow the scope rather than reason over
# an arbitrary truncated slice (which gives confidently-wrong answers).
DEFAULT_LIMIT = 50

_SCRUB_CACHE_SIZE = 512

# Most-urgent-first ranking for resource display status: down beats degraded
# beats working. Unknown statuses sort last, alphabetically, then by name.
_STATUS_URGENCY = {"Missing": 0, "Offline": 1, "Warning": 2}
_URGENCY_DEFAULT = 3


def resource_urgency(resource: dict) -> tuple:
    """Sort key putting the most broken digital workers first."""
    status = resource.get("displayStatus") or ""
    return (
        _STATUS_URGENCY.get(status, _URGENCY_DEFAULT),
        status,
        resource.get("name") or "",
    )


@dataclass(frozen=True)
class Ranked:
    """A tool's domain result: the FULL relevance-sorted records, untruncated.

    The split at the heart of the embeddable core (DESIGN Phase 8): a domain
    function returns ``Ranked`` (every record, already sorted, already scrubbed),
    and ``to_envelope`` is the one representation adapter that caps it to top-N
    and wraps it for an LLM client. A host embedding the engine consumes
    ``records`` directly and applies its own representation instead.

    ``meta`` carries domain-level extras that belong in the envelope's meta but
    are decided by the domain (e.g. ``deferred_unavailable`` when a fold-in read
    failed, or an ``unavailable`` note when an endpoint needs a newer estate) —
    so the list-tool adapter stays a pure cap-and-wrap. (A composite tool like
    estate_health nests a ``Ranked`` in one field and caps just that field.)
    """

    records: list[dict]
    sorted_by: str
    meta: dict = field(default_factory=dict)


def rank(
    rows: list[dict],
    sort_key: Callable[[dict], Any],
    sorted_by: str,
    reverse: bool = False,
) -> Ranked:
    """Sort *rows* by relevance into a ``Ranked`` — the domain step, no truncation.

    Sort keys use ``.get`` defaults so a row missing the sort field never raises.
    A domain method that needs ``meta`` extras constructs ``Ranked`` directly.
    """
    return Ranked(sorted(rows, key=sort_key, reverse=reverse), sorted_by)


def to_envelope(ranked: Ranked, limit: int | None = DEFAULT_LIMIT) -> dict:
    """Adapt a ``Ranked`` into the LLM-shaped, honestly-paginated envelope.

    Returns ``{"items": [...], "meta": {...}}`` where meta carries the full
    ``total``, the number ``returned``, whether the list was ``truncated``, and
    the ``sorted_by`` description so an LLM client knows it saw the top N of M.
    The domain's ``ranked.meta`` extras are merged in.

    ``limit`` of None (or negative) returns every record; otherwise the first
    ``limit`` after sorting. The domain's ``ranked.meta`` is merged first, so the
    structural pagination keys always win and can never be clobbered.
    """
    records = ranked.records
    total = len(records)
    items = records if limit is None or limit < 0 else records[:limit]
    return {
        "items": items,
        "meta": {
            **ranked.meta,
            "total": total,
            "returned": len(items),
            "truncated": len(items) < total,
            "sorted_by": ranked.sorted_by,
        },
    }


def envelope(
    rows: list[dict],
    sort_key: Callable[[dict], Any],
    sorted_by: str,
    limit: int | None = DEFAULT_LIMIT,
    reverse: bool = False,
) -> dict:
    """Rank *rows* and wrap them in the envelope — the rank+adapt composition.

    Kept as the convenience one-shot for callers that don't need the domain
    ``Ranked`` value on its own; equivalent to
    ``to_envelope(rank(rows, sort_key, sorted_by, reverse), limit)``.
    """
    return to_envelope(rank(rows, sort_key, sorted_by, reverse), limit)


def read_or_unavailable(read: Callable[[], dict], label: str) -> dict:
    """Run an optional read; on a transport/HTTP failure return an unavailable note.

    The insight tools layer a few non-essential `/dashboards` reads on top of
    their core data; a denied (no permission) or dropped (timeout, connection)
    one must degrade *visibly* rather than failing the whole tool. Returns the
    read's dict on success, or ``{"unavailable": "<label> failed: <error>"}`` —
    one shape every consumer can branch on. RequestException is the root of
    every error requests raises (HTTPError, Timeout, ConnectionError).
    """
    try:
        return read()
    except requests.RequestException as exc:
        return {"unavailable": f"{label} failed: {exc}"}


def validate_iso(value: str | None, field: str, required: bool = False) -> None:
    """Raise ValueError unless *value* is a parseable ISO date or datetime.

    LLM clients can pass malformed dates; the v7 filters would reject them
    server-side with an opaque 400 (and the mock would silently return an
    empty result). Failing loudly here tells the model exactly what it got
    wrong instead. Accepts anything datetime.fromisoformat does — a plain
    date, a full timestamp, a trailing Z.
    """
    if value is None:
        if required:
            raise ValueError(f"{field} is required (ISO format, e.g. 2026-03-01).")
        return
    try:
        datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(
            f"{field} must be an ISO date or datetime (e.g. 2026-03-01 or "
            f"2026-03-01T09:00:00); got {value!r}."
        ) from None


def require_window(start_date: str | None, end_date: str | None) -> None:
    """Validate a mandatory, correctly-ordered date window.

    The high-volume reads (queue items, sessions) refuse to run unbounded —
    queues run to millions of items — so the window is required, and a
    reversed window fails loudly rather than silently returning nothing.
    """
    validate_iso(start_date, "start_date", required=True)
    validate_iso(end_date, "end_date", required=True)
    # validate_iso(required=True) has already raised on None; this only narrows
    # the Optional for the type-checker.
    assert start_date is not None and end_date is not None
    # tzinfo is stripped for the ordering check only: comparing an aware bound
    # with a naive one raises TypeError, and a loud-but-wrong crash on a valid
    # window is worse than ignoring offsets in this sanity check. The values
    # themselves pass through to the API untouched.
    start = datetime.fromisoformat(start_date).replace(tzinfo=None)
    end = datetime.fromisoformat(end_date).replace(tzinfo=None)
    if start > end:
        raise ValueError(
            f"start_date {start_date!r} is after end_date {end_date!r} — swap the bounds."
        )


def validate_optional_window(start_date: str | None, end_date: str | None) -> None:
    """Validate an OPTIONAL date window: each bound ISO if given, ordered if both.

    Unlike require_window (the high-volume reads that refuse to run unbounded),
    this is for reads where a window only *narrows* an already-scoped result
    (one session's stage log) — so either bound may be omitted, but a malformed
    or reversed one still fails loudly rather than reaching the API as an opaque
    400 or silently returning nothing.
    """
    validate_iso(start_date, "start_date", required=False)
    validate_iso(end_date, "end_date", required=False)
    if start_date and end_date:
        start = datetime.fromisoformat(start_date).replace(tzinfo=None)
        end = datetime.fromisoformat(end_date).replace(tzinfo=None)
        if start > end:
            raise ValueError(
                f"start_date {start_date!r} is after end_date {end_date!r} — swap the bounds."
            )


def validate_choice(value: str, field: str, allowed: frozenset[str]) -> str:
    """Return the canonical casing of an enum value, or fail listing the choices.

    Case-insensitive and whitespace-tolerant on the way in ("exceptioned" is an
    obvious Exceptioned), canonical on the way out — the v7 filters are exact.
    """
    canonical = {choice.lower(): choice for choice in allowed}
    match = canonical.get(value.strip().lower()) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"{field} must be one of {', '.join(sorted(allowed))}; got {value!r}.")
    return match


def validate_uuid(value: str, field: str, hint: str = "") -> str:
    """Return *value* if it parses as a UUID; fail loudly with *hint* otherwise.

    For ids that cannot be name-resolved — queue items have no unscoped
    listing to resolve names against — the tool can still catch a model
    passing a key value or display text where the API needs the UUID.
    """
    try:
        UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        raise ValueError(
            f"{field} must be a UUID; got {value!r}.{f' {hint}' if hint else ''}"
        ) from None
    return str(value).strip()


def validate_positive_int(value: Any, field: str, hint: str = "") -> int:
    """Return *value* if it is an integer of 1 or higher; fail loudly otherwise.

    Counts like attempt numbers arrive as whatever JSON carried — a float, a
    quoted "3", a stray boolean — and the API would reject them server-side
    with an opaque 400. bool is excluded explicitly (True is an int in
    Python): a model passing true where a count belongs made a mistake worth
    naming, not coercing.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"{field} must be a positive integer (1 or higher); got {value!r}."
            f"{f' {hint}' if hint else ''}"
        )
    return value


# Blue Prism DataValue types for session start-up parameters (the PUT
# .../parameters body tags every value with one of these). The API is exact on
# casing, so values are canonicalised case-insensitively to these spellings.
DATA_VALUE_TYPES = frozenset(
    {
        "Binary",
        "Collection",
        "Date",
        "DateTime",
        "Flag",
        "Image",
        "Number",
        "Password",
        "RadioButtons",
        "Text",
        "Time",
        "TimeSpan",
    }
)


def validate_data_value(name: str, spec: Any) -> dict:
    """Validate and normalise a single Blue Prism DataValue spec.

    Shared by both session start-up parameters and queue-item data rows: both
    carry ``{"valueType": <type>, "value": <value>}`` with an optional
    ``additionalParameters``. A malformed shape or unknown valueType fails
    loudly naming *name* (the parameter/field context). Returns the normalised
    entry with canonical type casing. Collection values are validated
    recursively.
    """
    if not isinstance(spec, dict) or "valueType" not in spec or "value" not in spec:
        raise ValueError(
            f"parameter {name!r} must be an object with 'valueType' and "
            '\'value\' (e.g. {"valueType": "Text", "value": "hello"}).'
        )
    value_type = validate_choice(
        spec["valueType"], f"parameter {name!r} valueType", DATA_VALUE_TYPES
    )
    entry: dict[str, Any] = {"valueType": value_type, "value": spec["value"]}
    extra = spec.get("additionalParameters")
    if extra is not None:
        if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
            raise ValueError(
                f"parameter {name!r} additionalParameters must be an array of strings (or omitted)."
            )
        entry["additionalParameters"] = extra
    if value_type == "Collection" and isinstance(spec["value"], dict):
        _validate_collection_rows(name, spec["value"])
    return entry


def _validate_collection_rows(context: str, data: dict) -> None:
    """Recursively validate a DataCollection's rows structure."""
    rows = data.get("rows")
    if rows is None:
        return
    if not isinstance(rows, list):
        raise ValueError(f"{context} Collection 'rows' must be a list.")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{context} Collection rows[{i}] must be an object.")
        for field_name, field_spec in row.items():
            row[field_name] = validate_data_value(f"{context}.rows[{i}].{field_name}", field_spec)


def validate_session_parameters(parameters: Any) -> dict[str, dict] | None:
    """Validate and normalise process start-up parameters for the PUT body.

    Agents pass a mapping of parameter name to
    ``{"valueType": <Blue Prism type>, "value": <value>}`` (with an optional
    ``additionalParameters`` array of strings). A malformed shape or unknown
    valueType fails loudly here rather than as an opaque server-side 400, and
    the type casing is canonicalised to what the API expects. ``None`` means
    "no parameters" — the parameterless start path. Values pass through
    untouched: the model owns the typed payload, and the caller records only
    names and types in the audit (a value can be a Password).
    """
    if parameters is None:
        return None
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError(
            "parameters must be a non-empty object mapping each name to "
            '{"valueType": ..., "value": ...}.'
        )
    normalised: dict[str, dict] = {}
    for name, spec in parameters.items():
        normalised[name] = validate_data_value(name, spec)
    return normalised


def resolve_id(
    value: str,
    records: list[dict],
    entity: str,
    id_key: str = "id",
    name_key: str = "name",
) -> str:
    """Resolve a human name to the entity's id ("names in, UUIDs underneath").

    Agents speak in names ("the Invoices queue"); every v7 entity id is a UUID.
    A value that already parses as a UUID — or equals a record's id — passes
    straight through. Otherwise match the name case-insensitively and exactly:
    a miss fails loudly with the closest known names (so the model can correct
    itself instead of guessing), and a duplicate name fails listing every
    match's id (silently picking one would act on the wrong entity).
    """
    candidate = value.strip() if isinstance(value, str) else value
    if not candidate:
        raise ValueError(f"{entity} must be a name or id; got {value!r}.")
    try:
        UUID(str(candidate))
        return str(candidate)
    except ValueError:
        pass
    for record in records:
        if str(record.get(id_key)) == str(candidate):
            return str(candidate)

    wanted = str(candidate).casefold()
    matches = [r for r in records if str(r.get(name_key, "")).casefold() == wanted]
    if len(matches) == 1:
        return str(matches[0][id_key])
    if matches:
        listed = ", ".join(f"{m[name_key]} ({m[id_key]})" for m in matches)
        raise ValueError(
            f"{entity} name {value!r} is ambiguous — {len(matches)} match: "
            f"{listed}. Pass the id instead."
        )

    names = sorted({str(r[name_key]) for r in records if r.get(name_key)})
    close = get_close_matches(str(candidate), names, n=5, cutoff=0.6)
    hint = (
        f" Did you mean: {', '.join(close)}?"
        if close
        else f" Known {entity}s: {', '.join(names)}."
        if names
        else ""
    )
    raise ValueError(f"No {entity} named {value!r}.{hint}")


def make_cached_scrub(
    scrubber: Scrubber, maxsize: int = _SCRUB_CACHE_SIZE
) -> Callable[[str | None], str | None]:
    """An lru-cached, None-safe text scrub for the tool boundary.

    Scrubbing is deterministic and an LLM client typically re-reads the same
    rows across calls, so each distinct message runs through the backend (NER
    under Presidio) once. None and "" pass through untouched — item rows carry
    ``exceptionReason: null`` by design, and that must survive as null rather
    than becoming a scrubbed empty string.
    """

    @lru_cache(maxsize=maxsize)
    def _scrub(text: str) -> str:
        return scrubber.scrub(text).text

    def scrub_text(text: str | None) -> str | None:
        if not text:
            return text
        return _scrub(text)

    return scrub_text


# --- Queue-item validation (Tier 3, work injection) -------------------------

_QUEUE_ITEM_KEYS = frozenset(
    {"data", "deferredDate", "priority", "tags", "status", "sla", "processName", "isSuggested"}
)


def validate_queue_items(items: Any) -> list[dict]:
    """Validate and normalise a batch of queue items for the POST body.

    Each item is a dict of known keys only — an unknown key fails loudly
    naming it (a typo'd key silently dropped by the server is exactly the
    malformed-payload failure mode). Returns the normalised list ready for
    the API body.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list of objects.")
    normalised: list[dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{i}] must be an object.")
        unknown = set(item.keys()) - _QUEUE_ITEM_KEYS
        if unknown:
            raise ValueError(
                f"items[{i}] contains unknown key(s): {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(_QUEUE_ITEM_KEYS))}."
            )
        entry: dict[str, Any] = {}
        if "data" in item:
            _validate_item_data(i, item["data"])
            entry["data"] = item["data"]
        if "deferredDate" in item:
            validate_iso(item["deferredDate"], f"items[{i}].deferredDate")
            entry["deferredDate"] = item["deferredDate"]
        if "priority" in item:
            if isinstance(item["priority"], bool) or not isinstance(item["priority"], int):
                raise ValueError(
                    f"items[{i}].priority must be an integer; got {item['priority']!r}."
                )
            entry["priority"] = item["priority"]
        if "sla" in item:
            if item["sla"] is not None:
                if isinstance(item["sla"], bool) or not isinstance(item["sla"], int):
                    raise ValueError(
                        f"items[{i}].sla must be an integer or null; got {item['sla']!r}."
                    )
            entry["sla"] = item["sla"]
        if "tags" in item:
            if not isinstance(item["tags"], list) or not all(
                isinstance(t, str) for t in item["tags"]
            ):
                raise ValueError(f"items[{i}].tags must be an array of strings.")
            entry["tags"] = item["tags"]
        if "isSuggested" in item:
            if not isinstance(item["isSuggested"], bool):
                raise ValueError(
                    f"items[{i}].isSuggested must be a boolean; got {item['isSuggested']!r}."
                )
            entry["isSuggested"] = item["isSuggested"]
        if "status" in item:
            if not isinstance(item["status"], str):
                raise ValueError(f"items[{i}].status must be a string; got {item['status']!r}.")
            entry["status"] = item["status"]
        if "processName" in item:
            if not isinstance(item["processName"], str):
                raise ValueError(
                    f"items[{i}].processName must be a string; got {item['processName']!r}."
                )
            entry["processName"] = item["processName"]
        normalised.append(entry)
    return normalised


def _validate_item_data(index: int, data: Any) -> None:
    """Validate a queue item's DataCollection payload."""
    if not isinstance(data, dict):
        raise ValueError(f"items[{index}].data must be an object (DataCollection).")
    _validate_collection_rows(f"items[{index}].data", data)
