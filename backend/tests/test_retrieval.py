"""BM25 evidence retrieval tests (spec §11) — pure, no network, real corpus.

Relevance is asserted against the actual data/evidence/evidence_corpus.json
snippets: the scripted demo queries must surface their load-bearing snippets
(kiosk-vs-home BP sourcing, ACE-inhibitor dizziness, stale-lab sequencing),
and the score floor must keep off-topic queries honestly EMPTY.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.retrieval import (
    DEFAULT_SCORE_FLOOR,
    EvidenceIndex,
    clear_index_cache,
    get_index,
    load_corpus,
    retrieve,
    tokenize,
)

CORPUS_PATH = Path(__file__).resolve().parents[2] / "data" / "evidence" / "evidence_corpus.json"


@pytest.fixture(scope="module")
def index() -> EvidenceIndex:
    return get_index(CORPUS_PATH)


class TestCorpusLoading:
    def test_corpus_loads_all_snippets(self):
        snippets = load_corpus(CORPUS_PATH)
        assert len(snippets) == 30
        ids = [s.id for s in snippets]
        assert len(set(ids)) == 30
        assert all(s.id.startswith("ev-") for s in snippets)
        assert all(s.title and s.body and s.topic_tags for s in snippets)

    def test_index_is_cached_per_path(self):
        clear_index_cache()
        first = get_index(CORPUS_PATH)
        assert get_index(CORPUS_PATH) is first

    def test_empty_corpus_rejected(self):
        with pytest.raises(ValueError):
            EvidenceIndex([])


class TestRelevance:
    def test_kiosk_home_bp_query_returns_kiosk_snippet(self, index):
        hits = index.retrieve(
            "Set up reliable blood pressure monitoring — patient checks BP at a "
            "pharmacy kiosk and has no home monitor",
            k=3,
        )
        assert hits, "kiosk/home-BP query must retrieve evidence"
        assert hits[0].id == "ev-022"  # 'Pharmacy kiosk and public blood pressure machines'
        top_ids = {h.id for h in hits}
        # The home-monitoring protocol / patient-capability snippets rank with it.
        assert top_ids & {"ev-021", "ev-023"}

    def test_ace_inhibitor_dizziness_query_returns_side_effect_snippet(self, index):
        hits = index.retrieve(
            "patient stopped lisinopril ACE inhibitor because of dizziness side effect",
            k=3,
        )
        assert hits and hits[0].id == "ev-006"  # 'Dizziness in patients taking ACE inhibitors'

    def test_stale_labs_query_returns_sequencing_snippet(self, index):
        hits = index.retrieve(
            "obtain updated potassium and kidney function labs before any medication adjustment",
            k=3,
        )
        assert hits and hits[0].id == "ev-012"  # 'Rechecking labs before restarting...'

    def test_followup_query_returns_followup_snippet(self, index):
        hits = index.retrieve("schedule short-interval follow-up visit after a medication change", k=3)
        assert hits and hits[0].id in {"ev-028", "ev-030"}

    def test_scores_descend_and_ids_are_real(self, index):
        hits = index.retrieve("home blood pressure monitoring", k=3)
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)
        corpus_ids = {s.id for s in index.snippets}
        assert all(h.id in corpus_ids for h in hits)


class TestScoreFloorHonesty:
    def test_off_topic_query_returns_empty(self, index):
        assert index.retrieve("banana quantum spaceship parade", k=3) == []

    def test_generic_filler_stays_below_floor(self, index):
        # Generic clinical filler scores ~3 at best — below the 4.0 floor.
        assert index.retrieve("the patient was seen today", k=3) == []

    def test_empty_query_returns_empty(self, index):
        assert index.retrieve("", k=3) == []
        assert index.retrieve("!!! ???", k=3) == []

    def test_floor_is_overridable(self, index):
        # With the floor removed, even weak matches come back (k respected).
        hits = index.retrieve("the patient was seen today", k=2, score_floor=0.0)
        assert len(hits) == 2

    def test_k_limits_results(self, index):
        query = "home blood pressure monitoring pharmacy kiosk validated cuff"
        assert len(index.retrieve(query, k=1)) == 1
        assert len(index.retrieve(query, k=0)) == 0


class TestModuleHelpers:
    def test_tokenize(self):
        assert tokenize("Home BP — monitoring, 150/95!") == ["home", "bp", "monitoring", "150", "95"]

    def test_module_level_retrieve_with_explicit_path(self):
        hits = retrieve("pharmacy kiosk blood pressure reliability", k=2, path=CORPUS_PATH)
        assert hits and hits[0].id == "ev-022"
        assert all(h.score > DEFAULT_SCORE_FLOOR for h in hits)
