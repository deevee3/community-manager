"""Pipeline: one intake item in -> FAQ answer and/or flag out.

Flow per item:
  1. Log to intake.
  2. FAQ retrieval: strong match -> answer (Discord: reply text returned to
     the caller; email: surfaced to Telegram as a draft card). Logged either
     way.
  3. Classifier: verdict -> flag filed + Telegram card queued.
  4. Email that is neither FAQ nor flagged -> needs_human flag, because an
     unanswered inbox is how things get missed.
  5. Anything else -> ignored (counted in stats).

The pipeline performs NO enforcement. Its only outward effects are: an FAQ
answer where the question was asked, and Telegram cards to the admin chat.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from . import classifier, flags
from .database import Database
from .discord_intake import IntakeItem
from .faq import FaqIndex

log = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    intake_id: int
    outcome: str                      # faq_answered | flagged | ignored
    faq_answer: str = ""              # set when outcome == faq_answered (discord)
    faq_score: float = 0.0
    flag_id: int | None = None
    flag_category: str = ""
    notes: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, db: Database, faq_index: FaqIndex, *,
                 faq_threshold: float = 0.55,
                 ollama_url: str = "", ollama_model: str = ""):
        self.db = db
        self.faq = faq_index
        self.threshold = faq_threshold
        self.ollama_url = ollama_url
        self.ollama_model = ollama_model
        self._recent_by_author: Counter = Counter()

    def process(self, item: IntakeItem) -> PipelineResult:
        text = (item.text or "").strip()
        intake_id = self.db.log_intake(item.source, item.author, item.channel, text)
        result = PipelineResult(intake_id=intake_id, outcome="ignored")
        if not text:
            self.db.set_intake_outcome(intake_id, "ignored")
            return result

        # --- FAQ first: a good answer beats a flag every time.
        match = self.faq.best_match(text)
        if match and match.score >= self.threshold:
            self.db.faq_record_hit(match.faq_id)
            result.outcome = "faq_answered"
            result.faq_answer = match.answer
            result.faq_score = match.score
            self.db.set_intake_outcome(intake_id, "faq_answered")
            log.info("Intake #%d FAQ-answered (score %.2f, faq #%d)",
                     intake_id, match.score, match.faq_id)
            return result

        # --- Classifier.
        key = f"{item.source}:{item.author}"
        self._recent_by_author[key] += 1
        verdict = classifier.classify(
            text, repeat_count=self._recent_by_author[key]
        )
        verdict = classifier.ollama_rescore(
            text, verdict, self.ollama_url, self.ollama_model
        )

        # Unanswered email is never silently dropped.
        if verdict is None and item.source.endswith("email"):
            verdict = classifier.needs_human(text, reason="email without FAQ match")

        if verdict is not None:
            flag_id = flags.create_flag(
                self.db,
                intake_id=intake_id,
                source=item.source,
                author=item.author,
                channel=item.channel,
                text=text,
                verdict=verdict,
            )
            result.outcome = "flagged"
            result.flag_id = flag_id
            result.flag_category = verdict.category
            self.db.set_intake_outcome(intake_id, "flagged")
            return result

        self.db.set_intake_outcome(intake_id, "ignored")
        return result
