"""Pluggable PII scrubbing.

Decision (2026-06-09, extended 2026-06-10): scrubbing is pluggable behind the
`Scrubber` protocol, with THREE shipped tiers:

- `NullScrubber` — pass-through. Choosing it is an explicit, auditable
  deployment decision, never a silent fallback.
- `RegexScrubber` — zero-dependency pattern scrubbing for the entity types
  that dominate UK financial-services estates (NI numbers, sort codes,
  account numbers, card PANs with a Luhn check, emails, phone numbers).
  The base install therefore has a credible redaction story without the
  heavyweight `[pii]` extra.
- `PresidioScrubber` — Presidio + spaCy NER behind the `[pii]` extra, for
  free-text entities (names, addresses) regexes cannot catch. It reuses the
  same regex pattern set as score-1.0 recognizers, so exact domain patterns
  always beat NER detections on the same span.

Backend selection is explicit config (`BPConfig.pii_backend`) and FAILS LOUD:
if the requested backend cannot load, `build_scrubber` raises rather than
degrading — PII must never flow to a model because of a packaging mistake.

Replacement is a pluggable operator (`Replacement`): the default emits typed
tokens (``[UK_NI_NUMBER]``); a correlation-preserving operator (``[PERSON_1]``,
``[PERSON_2]``) can be slotted in later without touching the protocol.

Audit rule (inherited): log entity *types* found, never raw content; to files,
never stdout (stdio transport reserves stdout for JSON-RPC). The lru-cached
per-message wrapper lands with the tool boundary (Phase 4) — `ScrubResult` is
frozen and hashable for exactly that reason.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .config import BPConfig

_log = logging.getLogger("blue_prism_mcp.pii")

# Replacement operator: (entity_type, matched_text) -> replacement text.
Replacement = Callable[[str, str], str]


def typed_token(entity_type: str, _matched: str) -> str:
    """Default replacement: the entity type as a bracketed token."""
    return f"[{entity_type}]"


@dataclass(frozen=True)
class ScrubResult:
    """Scrubbed text plus the entity types found (sorted, deduplicated).

    Entity types — never raw content — are what the audit trail records.
    Frozen and hashable so a tool-boundary cache can hold results directly.
    """

    text: str
    entity_types: tuple[str, ...] = ()


@runtime_checkable
class Scrubber(Protocol):
    """Removes personal data from free-text before it leaves the process."""

    def scrub(self, text: str) -> ScrubResult: ...


class ScrubberUnavailableError(RuntimeError):
    """The configured scrubber backend cannot be constructed.

    Raised instead of falling back: a deployment that asked for redaction
    must not silently run without it.
    """


class NullScrubber:
    """Pass text straight through, reporting no entities.

    Used when a deployment explicitly opts out (`pii_backend = "null"`,
    the default for the light base install).
    """

    def scrub(self, text: str) -> ScrubResult:
        return ScrubResult(text=text)


# The built-in pattern set. Ordered: earlier patterns win where spans overlap,
# and deployment-supplied custom patterns are prepended so domain identifiers
# always beat the generic ones. False positives over-redact (an 8-digit job
# number becomes [UK_ACCOUNT_NUMBER]) — that costs agent context, not safety,
# which is the right default for regulated estates.
_BUILTIN_PATTERNS: tuple[tuple[str, str], ...] = (
    ("EMAIL_ADDRESS", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("UK_NI_NUMBER", r"\b[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b"),
    # Candidate PANs (13–19 digits, optional space/hyphen separators) are only
    # treated as cards when they pass the Luhn check — see _luhn_ok.
    ("CARD_NUMBER", r"\b(?:\d[ -]?){12,18}\d\b"),
    ("UK_SORT_CODE", r"\b\d{2}[- ]\d{2}[- ]\d{2}\b"),
    ("UK_ACCOUNT_NUMBER", r"\b\d{8}\b"),
    ("UK_PHONE", r"(?:\+44[\s-]?|\b0)\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}\b"),
)


def _luhn_ok(candidate: str) -> bool:
    """Luhn checksum over the digits of *candidate* (separators stripped)."""
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class RegexScrubber:
    """Zero-dependency scrubbing of pattern-shaped PII.

    Covers the identifier-style entities that dominate FS payloads. It cannot
    catch free-text names or addresses — that is the Presidio tier's job.
    """

    def __init__(
        self,
        custom_patterns: tuple[tuple[str, str], ...] = (),
        replacement: Replacement = typed_token,
    ) -> None:
        # Custom (deployment) patterns first: first-match-wins on overlap.
        self._patterns = [
            (name, re.compile(pattern)) for name, pattern in (*custom_patterns, *_BUILTIN_PATTERNS)
        ]
        self._replacement = replacement

    def scrub(self, text: str) -> ScrubResult:
        if not text or not text.strip():
            return ScrubResult(text=text)

        # Collect candidate spans in pattern-priority order, keeping each one
        # only where it does not overlap a span already claimed.
        claimed: list[tuple[int, int, str]] = []  # (start, end, entity_type)
        for name, pattern in self._patterns:
            for m in pattern.finditer(text):
                if name == "CARD_NUMBER" and not _luhn_ok(m.group()):
                    continue
                if any(m.start() < end and start < m.end() for start, end, _ in claimed):
                    continue
                claimed.append((m.start(), m.end(), name))

        if not claimed:
            return ScrubResult(text=text)

        # Rebuild in document order so a future numbering operator counts
        # entities the way a reader meets them ([PERSON_1] appears first).
        parts: list[str] = []
        cursor = 0
        for start, end, name in sorted(claimed):
            parts.append(text[cursor:start])
            parts.append(self._replacement(name, text[start:end]))
            cursor = end
        parts.append(text[cursor:])
        out = "".join(parts)

        entity_types = tuple(sorted({name for _, _, name in claimed}))
        _log.info("PII scrubber (regex): redacted %s (%d chars in)", list(entity_types), len(text))
        return ScrubResult(text=out, entity_types=entity_types)


class PresidioScrubber:
    """Presidio + spaCy NER scrubbing, behind the `[pii]` extra.

    Adds free-text entity detection (PERSON, LOCATION, …) on top of the same
    pattern set the regex tier uses; the patterns run as score-1.0 recognizers
    so an exact domain match always wins over an NER guess on the same span.
    """

    def __init__(
        self,
        spacy_model: str = "en_core_web_sm",
        custom_patterns: tuple[tuple[str, str], ...] = (),
        replacement: Replacement = typed_token,
    ) -> None:
        # The stdio transport reserves stdout for JSON-RPC: quieten the heavy
        # libraries BEFORE importing them — their import-time logging defaults
        # are noisy.
        for noisy in ("presidio-analyzer", "presidio-anonymizer", "spacy"):
            logging.getLogger(noisy).setLevel(logging.ERROR)
        try:
            from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
            from presidio_analyzer.nlp_engine import NlpEngineProvider
            from presidio_anonymizer import AnonymizerEngine
            from presidio_anonymizer.entities import OperatorConfig
        except ImportError as exc:
            raise ScrubberUnavailableError(
                "pii_backend is 'presidio' but the Presidio libraries are not "
                "installed — install the package with the [pii] extra "
                '(pip install "blue-prism-mcp[pii]").'
            ) from exc

        self._operator_config = OperatorConfig
        self._replacement = replacement
        # Check the model is installed BEFORE building the engine: Presidio
        # otherwise tries to download a missing model at startup (a surprise
        # network call that also writes to stdout, which stdio reserves for
        # JSON-RPC) and exits the process when that fails.
        import spacy

        if not spacy.util.is_package(spacy_model):
            raise ScrubberUnavailableError(
                f"pii_backend is 'presidio' but the spaCy model {spacy_model!r} "
                "is not installed — run: python -m spacy download " + spacy_model
            )
        nlp_engine = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": spacy_model}],
            }
        ).create_engine()
        self._analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
        for name, pattern in (*custom_patterns, *_BUILTIN_PATTERNS):
            # score=1.0: exact-regex domain patterns always win over spaCy NER
            # detections (e.g. LOCATION) on the same span.
            self._analyzer.registry.add_recognizer(
                PatternRecognizer(
                    supported_entity=name,
                    patterns=[Pattern(name=name, regex=pattern, score=1.0)],
                )
            )
        self._anonymizer = AnonymizerEngine()

    def scrub(self, text: str) -> ScrubResult:
        if not text or not text.strip():
            return ScrubResult(text=text)

        results = self._analyzer.analyze(text=text, language="en")
        results = [
            r
            for r in results
            if not (r.entity_type == "CARD_NUMBER" and not _luhn_ok(text[r.start : r.end]))
        ]
        if not results:
            return ScrubResult(text=text)

        entity_types = tuple(sorted({r.entity_type for r in results}))
        replacement = self._replacement
        operators = {
            et: self._operator_config(
                "custom", {"lambda": (lambda matched, _et=et: replacement(_et, matched))}
            )
            for et in entity_types
        }
        anonymized = self._anonymizer.anonymize(
            text=text, analyzer_results=results, operators=operators
        )
        # Audit trail: entity types and length only — never raw content.
        _log.info(
            "PII scrubber (presidio): redacted %s (%d chars in)", list(entity_types), len(text)
        )
        return ScrubResult(text=anonymized.text, entity_types=entity_types)


def build_scrubber(config: BPConfig, replacement: Replacement = typed_token) -> Scrubber:
    """Construct the scrubber the deployment asked for — or refuse to start.

    No fallback chain: an unknown or unloadable backend raises so the failure
    happens at startup, visibly, not at the first message containing PII.
    """
    backend = config.pii_backend
    if backend == "null":
        return NullScrubber()
    if backend == "regex":
        return RegexScrubber(custom_patterns=config.pii_custom_patterns, replacement=replacement)
    if backend == "presidio":
        return PresidioScrubber(
            spacy_model=config.pii_spacy_model,
            custom_patterns=config.pii_custom_patterns,
            replacement=replacement,
        )
    raise ScrubberUnavailableError(
        f"Unknown pii_backend {backend!r} — expected 'null', 'regex' or 'presidio'."
    )
