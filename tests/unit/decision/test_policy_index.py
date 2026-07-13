"""Tests for services/decision/service/policy_index.py (N5).

Tested against the real policy.md (load_policy_text()), not an artificial
fixture - same principle as using sample-invoices.json directly elsewhere.
Retrieval assertions are relevance-focused (does section_ids contain what
it must, and exclude what it must not), never an exact section-count
assertion - policy.md may grow new sections later without breaking these.
"""

from __future__ import annotations

from services.decision.service.policy_index import (
    _normalize,
    parse_policy_index,
    retrieve_relevant_policy_text,
)
from services.decision.service.policy_loader import load_policy_text
from shared.contracts.models import Category
from tests.support.decision_fixtures import clean_invoice

REAL_POLICY = load_policy_text()


# --- parse_policy_index() ---------------------------------------------------


def test_parse_real_policy_extracts_preamble_and_seven_sections() -> None:
    index = parse_policy_index(REAL_POLICY)

    assert "Northwind Components" in index.preamble
    assert len(index.sections) == 7


def test_parse_real_policy_assigns_stable_section_ids() -> None:
    index = parse_policy_index(REAL_POLICY)

    section_ids = {s.section_id for s in index.sections}
    assert "meals_entertainment" in section_ids
    assert "travel" in section_ids
    assert "hardware" in section_ids
    assert "global_rules" in section_ids


def test_parse_policy_with_no_section_headers_becomes_entirely_preamble() -> None:
    """Safety property: a Dapr-overridden policy_text (F7/M13) that doesn't
    follow the documented '## ' format must never silently lose content -
    it all becomes preamble, always included in full."""
    index = parse_policy_index("Just some plain text with no headers at all.")

    assert index.sections == []
    assert "plain text" in index.preamble


# --- retrieve_relevant_policy_text() - deterministic floor ------------------


def test_retrieval_always_includes_global_rules_and_autonomy_sections() -> None:
    index = parse_policy_index(REAL_POLICY)
    invoice = clean_invoice(category=Category.MEALS)

    result = retrieve_relevant_policy_text(invoice, index)

    assert "global_rules" in result.section_ids
    assert any("autonomy" in section_id for section_id in result.section_ids)


def test_retrieval_includes_the_invoice_own_category_section() -> None:
    index = parse_policy_index(REAL_POLICY)

    for category, expected_section_id in (
        (Category.MEALS, "meals_entertainment"),
        (Category.TRAVEL, "travel"),
        (Category.SAAS, "software_saas"),
        (Category.HARDWARE, "hardware"),
    ):
        invoice = clean_invoice(category=category)
        result = retrieve_relevant_policy_text(invoice, index)
        assert expected_section_id in result.section_ids, category


def test_retrieval_excludes_unrelated_category_sections_for_a_clean_invoice() -> None:
    """A generic meals invoice, with no cross-referencing signal in its
    vendor/notes/line items, should not pull in Travel/SaaS/Hardware."""
    index = parse_policy_index(REAL_POLICY)
    invoice = clean_invoice(category=Category.MEALS)

    result = retrieve_relevant_policy_text(invoice, index)

    assert "travel" not in result.section_ids
    assert "software_saas" not in result.section_ids
    assert "hardware" not in result.section_ids


def test_retrieval_for_category_other_has_no_category_section() -> None:
    index = parse_policy_index(REAL_POLICY)
    invoice = clean_invoice(category=Category.OTHER)

    result = retrieve_relevant_policy_text(invoice, index)

    assert "meals_entertainment" not in result.section_ids
    assert "travel" not in result.section_ids
    assert "software_saas" not in result.section_ids
    assert "hardware" not in result.section_ids
    assert "global_rules" in result.section_ids


# --- retrieve_relevant_policy_text() - TF-IDF similarity layer -------------


def test_similarity_layer_pulls_in_a_genuinely_cross_referenced_section() -> None:
    """Proves the TF-IDF layer does real work, not just theory: a Travel
    invoice whose notes mention 'alcohol' (Meals-section vocabulary - see
    MEAL-03) should pull the Meals section in too, beyond the Travel
    category floor - a real relevance signal the deterministic floor alone
    would have missed entirely."""
    index = parse_policy_index(REAL_POLICY)
    invoice = clean_invoice(
        category=Category.TRAVEL,
        notes="client dinner included alcohol only, no receipt needed",
    )

    result = retrieve_relevant_policy_text(invoice, index)

    assert "meals_entertainment" in result.section_ids
    assert "travel" in result.section_ids  # still the deterministic category floor


def test_retrieval_handles_an_invoice_with_mostly_unmatched_vocabulary() -> None:
    """An invoice whose vendor/notes are made-up words with no match in
    policy.md at all - only invoice.category.value itself ("meals") is real
    vocabulary. Must not crash, and must still return exactly the
    deterministic floor (no unrelated section pulled in by noise)."""
    index = parse_policy_index(REAL_POLICY)
    invoice = clean_invoice(
        category=Category.MEALS,
        vendor="Zzyzxqvw Corp",
        notes="qwzxjklm flibbertigibbet",
    )

    result = retrieve_relevant_policy_text(invoice, index)

    assert "meals_entertainment" in result.section_ids
    assert "global_rules" in result.section_ids
    assert "hardware" not in result.section_ids


def test_normalize_of_an_empty_vector_returns_empty_without_dividing_by_zero() -> None:
    """Direct unit test of the private helper: a query vector with zero
    vocabulary overlap against the policy corpus (idf-filtered to nothing)
    produces an empty dict, not a division-by-zero crash."""
    assert _normalize({}) == {}


def test_retrieved_text_contains_preamble_and_only_included_sections() -> None:
    index = parse_policy_index(REAL_POLICY)
    invoice = clean_invoice(category=Category.MEALS)

    result = retrieve_relevant_policy_text(invoice, index)

    assert "Northwind Components" in result.text
    assert "Meals & Entertainment" in result.text
    assert "Travel" not in result.text
    assert "Hardware" not in result.text
