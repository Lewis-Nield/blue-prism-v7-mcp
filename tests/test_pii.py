"""Tests for the pluggable PII layer.

The regex tier and the factory's fail-loud posture are exercised everywhere;
the Presidio tier's tests skip when the [pii] extra is absent (CI installs it).
"""

from __future__ import annotations

import sys

import pytest

from blue_prism_mcp.config import BPConfig
from blue_prism_mcp.pii import (
    NullScrubber,
    PresidioScrubber,
    RegexScrubber,
    ScrubberUnavailableError,
    Scrubber,
    ScrubResult,
    _luhn_ok,
    build_scrubber,
)

# ---------------------------------------------------------------- protocol


def test_null_scrubber_passes_text_through_and_reports_nothing():
    result = NullScrubber().scrub("Maria Lopez, NI QQ 12 34 56 C")
    assert result == ScrubResult(text="Maria Lopez, NI QQ 12 34 56 C", entity_types=())


def test_scrubbers_satisfy_the_protocol():
    assert isinstance(NullScrubber(), Scrubber)
    assert isinstance(RegexScrubber(), Scrubber)


def test_scrub_result_is_hashable_for_the_phase4_cache():
    a = ScrubResult(text="x", entity_types=("EMAIL_ADDRESS",))
    b = ScrubResult(text="x", entity_types=("EMAIL_ADDRESS",))
    assert hash(a) == hash(b) and a == b


# ------------------------------------------------------------------- luhn


def test_luhn_accepts_a_valid_pan_and_rejects_an_invalid_one():
    assert _luhn_ok("4111 1111 1111 1111")
    # A PAN whose doubled digits exceed 9 exercises the subtract-9 step.
    assert _luhn_ok("5500 0000 0000 0004")
    assert not _luhn_ok("4111 1111 1111 1112")


def test_luhn_rejects_digit_runs_outside_pan_lengths():
    assert not _luhn_ok("4111")
    assert not _luhn_ok("0" * 20)


def test_luhn_accepts_the_13_and_19_digit_pan_length_bounds():
    assert _luhn_ok("4222222222222")  # 13-digit Visa test PAN
    assert _luhn_ok("6304950600000000007")  # 19-digit Maestro-length PAN


# ------------------------------------------------------------ regex tier


@pytest.fixture()
def regex_scrubber() -> RegexScrubber:
    return RegexScrubber()


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("contact maria.lopez@example.co.uk today", "[EMAIL_ADDRESS]"),
        ("applicant NI QQ 12 34 56 C on file", "[UK_NI_NUMBER]"),
        ("card 4111 1111 1111 1111 declined", "[CARD_NUMBER]"),
        ("sort code 20-45-67 missing", "[UK_SORT_CODE]"),
        ("sort code 20 45 67 missing", "[UK_SORT_CODE]"),
        ("sort code 204567 missing", "[UK_SORT_CODE]"),
        ("account 12345678 frozen", "[UK_ACCOUNT_NUMBER]"),
        ("call +44 7911 123456 before 5", "[UK_PHONE]"),
        ("call 07911 123456 before 5", "[UK_PHONE]"),
    ],
)
def test_regex_scrubber_redacts_each_builtin_entity(regex_scrubber, text, token):
    result = regex_scrubber.scrub(text)
    assert token in result.text
    assert result.entity_types == (token.strip("[]"),)


def test_regex_scrubber_leaves_clean_text_untouched(regex_scrubber):
    text = "Process Invoices finished: 14 items worked, 0 exceptions"
    assert regex_scrubber.scrub(text) == ScrubResult(text=text)


@pytest.mark.parametrize("text", ["", "   "])
def test_regex_scrubber_short_circuits_blank_input(regex_scrubber, text):
    assert regex_scrubber.scrub(text) == ScrubResult(text=text)


def test_regex_scrubber_rejects_a_luhn_invalid_card(regex_scrubber):
    text = "ref 4111 1111 1111 1112 is not a card"
    assert regex_scrubber.scrub(text) == ScrubResult(text=text)


def test_regex_scrubber_dedupes_and_sorts_entity_types(regex_scrubber):
    result = regex_scrubber.scrub("a@b.com wrote to c@d.com about NI QQ 12 34 56 C")
    assert result.entity_types == ("EMAIL_ADDRESS", "UK_NI_NUMBER")
    assert result.text.count("[EMAIL_ADDRESS]") == 2


def test_regex_scrubber_earlier_pattern_claims_overlapping_spans(regex_scrubber):
    # The NI number contains "12 34 56", a valid sort-code shape; the NI
    # pattern is earlier in the set so it claims the span whole.
    result = regex_scrubber.scrub("holder QQ 12 34 56 C")
    assert result.entity_types == ("UK_NI_NUMBER",)
    assert "[UK_SORT_CODE]" not in result.text


def test_regex_scrubber_keeps_scanning_past_a_luhn_rejected_card(regex_scrubber):
    # A rejected candidate must not stop the scan: the valid card after the
    # invalid one is still redacted.
    result = regex_scrubber.scrub("bad 4111 1111 1111 1112 good 4111 1111 1111 1111")
    assert result.entity_types == ("CARD_NUMBER",)
    assert "4111 1111 1111 1111" not in result.text
    assert "4111 1111 1111 1112" in result.text


def test_regex_scrubber_keeps_scanning_past_an_overlapped_match():
    # The first email is inside a span a custom pattern already claimed; the
    # second, elsewhere, must still be redacted.
    scrubber = RegexScrubber(custom_patterns=(("QUOTED_REF", r"<a@b\.com>"),))
    result = scrubber.scrub("<a@b.com> then c@d.com")
    assert result.text == "[QUOTED_REF] then [EMAIL_ADDRESS]"
    assert result.entity_types == ("EMAIL_ADDRESS", "QUOTED_REF")


def test_regex_scrubber_adjacent_spans_do_not_count_as_overlapping():
    # Touching (end == start) is not overlap, whichever side claims first.
    for patterns in ((("A", "AAA"), ("B", "BBB")), (("B", "BBB"), ("A", "AAA"))):
        result = RegexScrubber(custom_patterns=patterns).scrub("xAAABBBx")
        assert result.text == "x[A][B]x"
        assert result.entity_types == ("A", "B")


def test_regex_scrubber_audit_log_names_types_but_never_content(caplog):
    # The audit-trail contract: entity types are logged, raw PII never is.
    with caplog.at_level("INFO", logger="blue_prism_mcp.pii"):
        RegexScrubber().scrub("mail maria@example.com about NI QQ 12 34 56 C")
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "EMAIL_ADDRESS" in messages and "UK_NI_NUMBER" in messages
    assert "maria@example.com" not in messages and "QQ 12 34 56 C" not in messages


def test_regex_scrubber_phone_outranks_the_compact_sort_code(regex_scrubber):
    # With sort-code separators optional, the trailing six digits of a phone
    # number are a sort-code shape — the phone pattern must claim them first.
    result = regex_scrubber.scrub("call 07911 123456 and quote 204567")
    assert result.text == "call [UK_PHONE] and quote [UK_SORT_CODE]"
    assert result.entity_types == ("UK_PHONE", "UK_SORT_CODE")


def test_regex_scrubber_rejects_an_invalid_custom_pattern_loudly():
    with pytest.raises(ScrubberUnavailableError, match=r"'BAD_REF'.*\(unclosed"):
        RegexScrubber(custom_patterns=(("BAD_REF", "(unclosed"),))


def test_regex_scrubber_custom_patterns_beat_builtins():
    scrubber = RegexScrubber(custom_patterns=(("CLIENT_REF", r"\bCLT-\d{8}\b"),))
    result = scrubber.scrub("see CLT-12345678 for details")
    assert result.text == "see [CLIENT_REF] for details"
    assert result.entity_types == ("CLIENT_REF",)


def test_regex_scrubber_replacement_operator_seam_supports_pseudonyms():
    # The correlation-preserving operator planned for later: numbered tokens,
    # stable per distinct value within one call.
    seen: dict[str, int] = {}

    def numbered(entity_type: str, matched: str) -> str:
        n = seen.setdefault(matched, len(seen) + 1)
        return f"[{entity_type}_{n}]"

    scrubber = RegexScrubber(replacement=numbered)
    result = scrubber.scrub("a@b.com again a@b.com then c@d.com")
    assert result.text == "[EMAIL_ADDRESS_1] again [EMAIL_ADDRESS_1] then [EMAIL_ADDRESS_2]"


# ---------------------------------------------------------------- factory


def test_build_scrubber_defaults_to_null():
    assert isinstance(build_scrubber(BPConfig()), NullScrubber)


def test_build_scrubber_regex_carries_config_custom_patterns():
    config = BPConfig(
        pii_backend="regex",
        pii_custom_patterns=(("CLIENT_REF", r"\bCLT-\d{8}\b"),),
    )
    scrubber = build_scrubber(config)
    assert scrubber.scrub("CLT-12345678").entity_types == ("CLIENT_REF",)


def test_build_scrubber_rejects_an_unknown_backend_loudly():
    with pytest.raises(ScrubberUnavailableError, match="Unknown pii_backend 'redactotron'"):
        build_scrubber(BPConfig(pii_backend="redactotron"))


def test_build_scrubber_presidio_fails_loud_when_extra_missing(monkeypatch):
    # None in sys.modules makes `import presidio_analyzer` raise ImportError,
    # simulating a base install without the [pii] extra.
    monkeypatch.setitem(sys.modules, "presidio_analyzer", None)
    with pytest.raises(ScrubberUnavailableError, match=r"\[pii\] extra"):
        build_scrubber(BPConfig(pii_backend="presidio"))


# ------------------------------------------------------------ presidio tier

presidio_installed = pytest.importorskip("presidio_analyzer", reason="[pii] extra not installed")


@pytest.fixture(scope="module")
def presidio_scrubber() -> PresidioScrubber:
    # Module-scoped: the spaCy model load is the expensive part.
    return PresidioScrubber()


def test_presidio_fails_loud_on_a_missing_spacy_model():
    with pytest.raises(ScrubberUnavailableError, match="no_such_model"):
        PresidioScrubber(spacy_model="no_such_model")


def test_presidio_scrubs_ner_and_pattern_entities_together(presidio_scrubber):
    result = presidio_scrubber.scrub("Maria Lopez emailed maria@example.com about the refund")
    assert "[PERSON]" in result.text
    assert "[EMAIL_ADDRESS]" in result.text
    assert "Maria" not in result.text and "maria@example.com" not in result.text
    assert "PERSON" in result.entity_types and "EMAIL_ADDRESS" in result.entity_types


def test_presidio_custom_pattern_wins_at_score_one(presidio_scrubber):
    scrubber = PresidioScrubber(custom_patterns=(("CLIENT_REF", r"\bCLT-\d{8}\b"),))
    result = scrubber.scrub("see CLT-12345678 for details")
    assert "[CLIENT_REF]" in result.text
    assert "CLIENT_REF" in result.entity_types


def test_presidio_rejects_an_invalid_custom_pattern_at_startup():
    # Presidio compiles recognizer regexes lazily; the constructor must not.
    with pytest.raises(ScrubberUnavailableError, match=r"'BAD_REF'"):
        PresidioScrubber(custom_patterns=(("BAD_REF", "(unclosed"),))


def test_presidio_leaves_clean_text_untouched(presidio_scrubber):
    text = "queue empty, run completed without exceptions"
    assert presidio_scrubber.scrub(text) == ScrubResult(text=text)


@pytest.mark.parametrize("text", ["", "   "])
def test_presidio_short_circuits_blank_input(presidio_scrubber, text):
    assert presidio_scrubber.scrub(text) == ScrubResult(text=text)


def test_presidio_drops_a_luhn_invalid_card_detection(presidio_scrubber):
    result = presidio_scrubber.scrub("ref 4111 1111 1111 1112")
    assert "CARD_NUMBER" not in result.entity_types


def test_build_scrubber_presidio_path():
    scrubber = build_scrubber(BPConfig(pii_backend="presidio"))
    assert isinstance(scrubber, PresidioScrubber)
