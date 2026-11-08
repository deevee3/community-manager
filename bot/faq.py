"""FAQ store + retrieval: TF-IDF cosine similarity, pure stdlib.

Why not FAISS/Chroma (see PRD §4): the FAQ base is small (tens to a few
hundred entries). TF-IDF cosine is exact and deterministic at that scale and
adds zero dependencies. This module is the only place retrieval lives, so
swapping in a vector DB later touches nothing else.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass

from .database import Database

log = logging.getLogger(__name__)

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faq_seed.json")

_TOKEN_RE = re.compile(r"[a-z0-9']+")

_STOPWORDS = {
    "a", "an", "the", "is", "are", "do", "does", "how", "what", "when",
    "where", "can", "i", "you", "to", "of", "in", "on", "for", "me", "my",
    "it", "this", "that", "and", "or", "if", "with", "about", "there",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


@dataclass
class FaqMatch:
    faq_id: int
    question: str
    answer: str
    score: float


class FaqIndex:
    """In-memory TF-IDF index over the faq table; rebuilt on demand."""

    def __init__(self, db: Database):
        self.db = db
        self._entries: list[dict] = []
        self._idf: dict[str, float] = {}
        self.refresh()

    def refresh(self) -> None:
        rows = self.db.faq_all()
        docs: list[Counter] = []
        df: Counter = Counter()
        self._entries = []
        for row in rows:
            tokens = tokenize(row["question"] + " " + (row["tags"] or ""))
            counts = Counter(tokens)
            docs.append(counts)
            for term in counts:
                df[term] += 1
            self._entries.append(
                {"id": row["id"], "question": row["question"],
                 "answer": row["answer"], "counts": counts}
            )
        n = max(len(docs), 1)
        self._idf = {
            term: math.log((1 + n) / (1 + freq)) + 1.0 for term, freq in df.items()
        }
        for entry in self._entries:
            entry["vector"] = self._vectorize(entry["counts"])
        log.info("FAQ index refreshed: %d entries", len(self._entries))

    def _vectorize(self, counts: Counter) -> dict[str, float]:
        vec = {t: c * self._idf.get(t, 1.0) for t, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def search(self, query: str, limit: int = 3) -> list[FaqMatch]:
        if not self._entries:
            return []
        qvec = self._vectorize(Counter(tokenize(query)))
        if not qvec:
            return []
        scored: list[FaqMatch] = []
        for entry in self._entries:
            evec: dict[str, float] = entry["vector"]
            score = sum(w * evec.get(t, 0.0) for t, w in qvec.items())
            if score > 0:
                scored.append(FaqMatch(entry["id"], entry["question"],
                                       entry["answer"], round(score, 4)))
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:limit]

    def best_match(self, query: str) -> FaqMatch | None:
        results = self.search(query, limit=1)
        return results[0] if results else None


def seed_faq_if_empty(db: Database) -> int:
    """Load bot/faq_seed.json into the faq table on first run."""
    if db.faq_count() > 0:
        return 0
    try:
        with open(SEED_PATH, encoding="utf-8") as fh:
            seed = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load FAQ seed (%s); starting empty", exc)
        return 0
    added = 0
    for item in seed:
        db.faq_add(item["question"], item["answer"], item.get("tags", ""))
        added += 1
    log.info("Seeded %d FAQ entries", added)
    return added
