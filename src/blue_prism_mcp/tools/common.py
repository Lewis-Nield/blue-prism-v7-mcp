"""Shared tool-layer plumbing: the envelope, loud validation, name→UUID
resolution, and the cached scrub boundary.

These are the contracts every tool carries (see DESIGN.md "Shared contract"):
list tools return a relevance-sorted, honestly-truncated envelope; dated
parameters fail loudly on malformed input; entity names resolve to UUIDs via
the (cached) list endpoints; and per-message PII scrubbing is cached because an
LLM client re-reads the same rows across calls.
"""

from __future__ import annotations

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


def envelope(
    rows: list[dict],
    sort_key: Callable[[dict], Any],
    sorted_by: str,
    limit: int | None = DEFAULT_LIMIT,
    reverse: bool = False,
) -> dict:
    """Wrap a list of records in a relevance-sorted, honestly-paginated envelope.

    Returns ``{"items": [...], "meta": {...}}`` where meta carries the full
    ``total``, the number ``returned``, whether the list was ``truncated``, and
    the ``sorted_by`` description so an LLM client knows it saw the top N of M.

    ``limit`` of None (or negative) returns every row; otherwise the first
    ``limit`` rows after sorting. Sort keys use ``.get`` defaults so a row
    missing the sort field never raises.
    """
    total = len(rows)
    ordered = sorted(rows, key=sort_key, reverse=reverse)
    items = ordered if limit is None or limit < 0 else ordered[:limit]
    return {
        "items": items,
        "meta": {
            "total": total,
            "returned": len(items),
            "truncated": len(items) < total,
            "sorted_by": sorted_by,
        },
    }


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
                    f"parameter {name!r} additionalParameters must be an array "
                    "of strings (or omitted)."
                )
            entry["additionalParameters"] = extra
        normalised[name] = entry
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
