"""Chunking + retrieval for policy.md (N5) - policy.md's own preamble already
specifies this exact task: retrieve only the relevant section(s) instead of
putting the whole policy in every prompt.

Section-level chunking (not per-rule-row): keeps related rules together for
the LLM (e.g. HW-01/HW-02 read better together than isolated), and avoids a
fragile per-table-row markdown parser for a document this size.

TF-IDF + cosine similarity, pure Python/stdlib (no numpy, no embedding API,
no vector DB) - a genuine vector-based retrieval layer sitting on top of a
deterministic floor (preamble + Global rules + Autonomy thresholds + the
invoice's own category section) that never depends on a similarity score.

An LLM-based reranker (retrieve candidates, ask a second LLM call "relevant
or not") was considered and rejected: policy.md has 7 short, fixed sections -
there's no ambiguity among many near-duplicate candidates for a reranker to
resolve, so a second LLM call would only add cost, latency, and a new
non-deterministic failure mode, with no precision gain over a numeric
threshold on a corpus this small.

Retrieval contract (guarantees):
- preamble is always returned.
- the "Global rules" and "Autonomy thresholds" sections are always returned.
- the invoice's own category section is always returned, if one exists
  (Category.OTHER has none - that's fine, not an error).
- the TF-IDF similarity layer can only ADD sections beyond the above - it
  never removes a deterministic section, regardless of its score.

Safety: retrieval quality only affects the agent's RECOMMENDATION - the
deterministic router (router/router.py) never sees policy text at all, so
imperfect retrieval can't compromise the correctness of a decision. See
decider.py's fail-safe: any retrieval exception falls back to the full
policy text unconditionally.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from shared.contracts.models import Category, Invoice

_SECTION_HEADER = re.compile(r"(?m)^## ")
_NUMBER_PREFIX = re.compile(r"^\d+\.\s*")
_WORD = re.compile(r"[a-z0-9]+")

_GLOBAL_RULES_KEYWORD = "global rules"
_AUTONOMY_KEYWORD = "autonomy thresholds"
_CATEGORY_KEYWORDS: dict[Category, str] = {
    Category.MEALS: "meals",
    Category.TRAVEL: "travel",
    Category.SAAS: "saas",
    Category.HARDWARE: "hardware",
}

# Chosen empirically against the real policy.md (see test_policy_index.py):
# a clean invoice in any of the 4 real categories scores exactly 0.0 cosine
# similarity against every OTHER category section (no spurious overlap at
# all - not just "below threshold"), while a genuine cross-reference (e.g.
# "alcohol" in a Travel invoice's notes, which is Meals-section vocabulary)
# scores ~0.20. 0.15 sits cleanly between "no real signal" (0.0) and "real
# signal" (>=0.19) observed on this document - not a guessed round number.
_SIMILARITY_THRESHOLD = 0.15


def _slugify(title: str) -> str:
    text = _NUMBER_PREFIX.sub("", title).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "section"


def _tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class PolicySection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    section_id: str
    title: str
    body: str


class RetrievedPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    section_ids: list[str]


@dataclass(frozen=True)
class PolicyIndex:
    """Built once per Decider (see decider.py) - parsing AND the TF-IDF
    vectors/idf weights are computed once here, not per decide() call.
    Per-request cost afterward is just one query vector + a handful of
    cosine similarities - negligible."""

    preamble: str
    sections: list[PolicySection]
    idf: dict[str, float]
    section_vectors: dict[str, dict[str, float]]


def parse_policy_index(policy_text: str) -> PolicyIndex:
    pieces = _SECTION_HEADER.split(policy_text)
    preamble = pieces[0].strip()
    sections: list[PolicySection] = []
    for piece in pieces[1:]:
        title, _, body = piece.partition("\n")
        sections.append(
            PolicySection(section_id=_slugify(title), title=title.strip(), body=body.strip())
        )

    idf, section_vectors = _build_tfidf(sections)
    return PolicyIndex(
        preamble=preamble, sections=sections, idf=idf, section_vectors=section_vectors
    )


def _build_tfidf(
    sections: list[PolicySection],
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    docs = {s.section_id: _tokenize(f"{s.title} {s.body}") for s in sections}
    n_docs = len(docs)
    doc_freq: Counter[str] = Counter()
    for tokens in docs.values():
        doc_freq.update(set(tokens))

    idf = {term: math.log((1 + n_docs) / (1 + df)) + 1 for term, df in doc_freq.items()}

    section_vectors: dict[str, dict[str, float]] = {}
    for section_id, tokens in docs.items():
        tf = Counter(tokens)
        vector = {term: count * idf[term] for term, count in tf.items()}
        section_vectors[section_id] = _normalize(vector)
    return idf, section_vectors


def _normalize(vector: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm == 0:
        return vector
    return {term: value / norm for term, value in vector.items()}


def _cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    shared_terms = a.keys() & b.keys()
    return sum(a[term] * b[term] for term in shared_terms)


def _build_query_vector(invoice: Invoice, idf: dict[str, float]) -> dict[str, float]:
    parts = [invoice.category.value, invoice.vendor, invoice.notes or ""]
    parts.extend(item.description for item in invoice.line_items)
    tokens = _tokenize(" ".join(parts))
    tf = Counter(tokens)
    vector = {term: count * idf[term] for term, count in tf.items() if term in idf}
    return _normalize(vector)


def retrieve_relevant_policy_text(invoice: Invoice, index: PolicyIndex) -> RetrievedPolicy:
    included: dict[str, PolicySection] = {}

    for section in index.sections:
        title_lower = section.title.lower()
        if _GLOBAL_RULES_KEYWORD in title_lower or _AUTONOMY_KEYWORD in title_lower:
            included[section.section_id] = section

    category_keyword = _CATEGORY_KEYWORDS.get(invoice.category)
    if category_keyword is not None:
        for section in index.sections:
            if category_keyword in section.title.lower():
                included[section.section_id] = section
                break

    query_vector = _build_query_vector(invoice, index.idf)
    for section in index.sections:
        if section.section_id in included:
            continue
        similarity = _cosine_similarity(query_vector, index.section_vectors[section.section_id])
        if similarity >= _SIMILARITY_THRESHOLD:
            included[section.section_id] = section

    ordered = [s for s in index.sections if s.section_id in included]
    text_parts = [index.preamble] + [f"## {s.title}\n{s.body}" for s in ordered]
    return RetrievedPolicy(
        text="\n\n".join(text_parts), section_ids=[s.section_id for s in ordered]
    )
