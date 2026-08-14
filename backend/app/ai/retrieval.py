"""BM25 retrieval over the synthetic evidence corpus (spec §11 — P0).

Serves care-plan ``evidence_refs``: retrieval must be GENUINELY produced —
never decorative. The corpus is the ~30-snippet synthetic guideline file at
``settings.data_dir/evidence/evidence_corpus.json`` (the same file the seed
loads into Postgres for the /api/evidence resolve route).

Pure and fast: the corpus + BM25 index are built once per path and cached at
module level; ``retrieve()`` does no I/O after the first call. A score floor
keeps refs honest — a query with no genuinely related snippet returns an
EMPTY list rather than the least-bad match (empty is allowed and honest).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

from app.config import get_settings
from app.schemas.core import EvidenceSnippet

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Empirically chosen for this corpus: on-topic care-plan queries score 5-15+
#: against their best snippet; generic/off-topic text stays under ~3.5.
DEFAULT_SCORE_FLOOR = 4.0


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass(frozen=True)
class RetrievedSnippet:
    """One retrieval hit: the snippet plus its BM25 score."""

    snippet: EvidenceSnippet
    score: float

    @property
    def id(self) -> str:
        return self.snippet.id

    @property
    def title(self) -> str:
        return self.snippet.title


class EvidenceIndex:
    """BM25 index over the evidence corpus. Build once, query many times."""

    def __init__(self, snippets: list[EvidenceSnippet]) -> None:
        if not snippets:
            raise ValueError("Evidence corpus is empty — retrieval cannot be honest.")
        self.snippets = snippets
        self._bm25 = BM25Okapi(
            [
                tokenize(f"{s.title} {s.body} {' '.join(s.topic_tags)}")
                for s in snippets
            ]
        )

    def retrieve(
        self, query: str, k: int = 3, *, score_floor: float = DEFAULT_SCORE_FLOOR
    ) -> list[RetrievedSnippet]:
        """Top-k snippets scoring above the floor, best first. May be empty."""
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            (RetrievedSnippet(snippet=s, score=float(score)) for s, score in zip(self.snippets, scores)),
            key=lambda hit: -hit.score,
        )
        return [hit for hit in ranked[: max(0, k)] if hit.score > score_floor]


# ---------------------------------------------------------------------------
# Module-level cache (per corpus path)
# ---------------------------------------------------------------------------

_indexes: dict[str, EvidenceIndex] = {}
_lock = threading.Lock()


def corpus_path() -> Path:
    return Path(get_settings().data_dir) / "evidence" / "evidence_corpus.json"


def load_corpus(path: str | Path | None = None) -> list[EvidenceSnippet]:
    """Parse the corpus file into contract EvidenceSnippet models."""
    resolved = Path(path) if path is not None else corpus_path()
    with open(resolved, encoding="utf-8") as fh:
        raw = json.load(fh)
    return [EvidenceSnippet.model_validate(entry) for entry in raw["snippets"]]


def get_index(path: str | Path | None = None) -> EvidenceIndex:
    """Cached EvidenceIndex for the given (or configured) corpus path."""
    resolved = str(Path(path) if path is not None else corpus_path())
    with _lock:
        index = _indexes.get(resolved)
        if index is None:
            index = EvidenceIndex(load_corpus(resolved))
            _indexes[resolved] = index
            logger.info(
                "Evidence index built: %d snippets from %s", len(index.snippets), resolved
            )
        return index


def clear_index_cache() -> None:
    with _lock:
        _indexes.clear()


def retrieve(
    query: str,
    k: int = 3,
    *,
    score_floor: float = DEFAULT_SCORE_FLOOR,
    path: str | Path | None = None,
) -> list[RetrievedSnippet]:
    """Module-level convenience: retrieve top-k over the configured corpus."""
    return get_index(path).retrieve(query, k, score_floor=score_floor)
