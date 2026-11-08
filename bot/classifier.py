"""Flag classifier: rule-based taxonomy scoring (+ optional Ollama pass).

Output is a *recommendation label* only. The classifier has no ability to act
on a message — it returns (category, confidence, recommendation) and the
pipeline files a flag for D'Vaughn to decide on in Telegram.

Categories: spam | scam | abuse | nsfw | off_topic | needs_human.
A message with no signal returns None (no flag) unless it arrives via email,
where anything that is not a clear FAQ match is filed as needs_human.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

RECOMMENDATIONS = {
    "spam": "Recommend: remove the message + warn the author (you do it manually).",
    "scam": "Recommend: remove the message + ban the author (you do it manually). URGENT — members may lose money.",
    "abuse": "Recommend: warn the author, timeout if it continues (you do it manually).",
    "nsfw": "Recommend: remove the message (you do it manually).",
    "off_topic": "Recommend: redirect to the right channel; usually no action needed.",
    "needs_human": "Recommend: read it yourself — the bot could not sort this confidently.",
}

# (category, regex patterns, weight per hit)
_RULES: list[tuple[str, list[str], float]] = [
    ("scam", [
        r"seed phrase", r"private key", r"send \d+.*(get|receive) \d+",
        r"double your (crypto|eth|btc|money)", r"guaranteed (profit|returns|income)",
        r"free (airdrop|giveaway).*(claim|connect wallet)", r"claim your (prize|reward|airdrop) now",
        r"verify your wallet", r"support team.*dm", r"dm (me|admin) (to|for) (verify|claim|unlock)",
    ], 0.45),
    ("spam", [
        r"(buy|join|check out).*(telegram|signal) group.*(profit|trading|signals)",
        r"make \$\d+.*(day|week|hour)", r"work from home.*\$\d+",
        r"(promo|discount|sale).*(code|link).*(hurry|limited)",
        r"https?://\S+.*https?://\S+.*https?://\S+",  # 3+ links in one message
    ], 0.4),
    ("abuse", [
        r"\b(idiot|moron|scumbag|loser)\b.*\byou\b", r"shut up",
        r"(kill yourself|kys)\b", r"you('re| are) (trash|pathetic|worthless)",
    ], 0.4),
    ("nsfw", [
        r"\b(onlyfans|xxx|nsfw|porn)\b", r"18\+.*(content|pics|link)",
    ], 0.45),
]

_URL_RE = re.compile(r"https?://\S+")


@dataclass
class Classification:
    category: str
    confidence: float
    recommendation: str
    signals: list[str]


def classify(text: str, *, repeat_count: int = 1) -> Classification | None:
    """Score a message against the taxonomy. None = nothing worth flagging."""
    lowered = text.lower()
    scores: dict[str, float] = {}
    signals: dict[str, list[str]] = {}

    for category, patterns, weight in _RULES:
        for pattern in patterns:
            if re.search(pattern, lowered):
                scores[category] = scores.get(category, 0.0) + weight
                signals.setdefault(category, []).append(pattern)

    # Burst/repetition is a spam signal even with innocent wording.
    if repeat_count >= 3:
        scores["spam"] = scores.get("spam", 0.0) + 0.35
        signals.setdefault("spam", []).append(f"same author x{repeat_count} similar messages")
    if len(_URL_RE.findall(text)) >= 2 and len(text) < 200:
        scores["spam"] = scores.get("spam", 0.0) + 0.2
        signals.setdefault("spam", []).append("link-heavy short message")

    if not scores:
        return None
    category, raw = max(scores.items(), key=lambda kv: kv[1])
    confidence = min(round(raw, 2), 0.99)
    if confidence < 0.35:
        return None
    return Classification(
        category=category,
        confidence=confidence,
        recommendation=RECOMMENDATIONS[category],
        signals=signals.get(category, []),
    )


def needs_human(text: str, reason: str = "unclassified") -> Classification:
    return Classification(
        category="needs_human",
        confidence=0.5,
        recommendation=RECOMMENDATIONS["needs_human"],
        signals=[reason],
    )


def ollama_rescore(text: str, result: Classification | None,
                   ollama_url: str, model: str) -> Classification | None:
    """Optional second opinion from a local Ollama model (Mac Studio).

    Best-effort: any failure returns the rule-based result unchanged. The
    model may only *raise/lower confidence within the same taxonomy* — it
    cannot create new action types, and nothing here executes anything.
    """
    if not ollama_url or not model:
        return result
    try:
        prompt = (
            "Classify this community message into exactly one category: "
            "spam, scam, abuse, nsfw, off_topic, none. Reply with JSON only: "
            '{"category": "...", "confidence": 0.0-1.0}\n\nMessage:\n' + text[:1000]
        )
        payload = json.dumps(
            {"model": model, "prompt": prompt, "stream": False}
        ).encode("utf-8")
        req = urllib.request.Request(
            ollama_url.rstrip("/") + "/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        verdict = json.loads(data.get("response", "{}"))
        category = str(verdict.get("category", "none"))
        confidence = float(verdict.get("confidence", 0.0))
        if category in RECOMMENDATIONS and confidence >= 0.6:
            return Classification(
                category=category,
                confidence=min(confidence, 0.99),
                recommendation=RECOMMENDATIONS[category],
                signals=["ollama re-score"],
            )
        if category == "none" and result is not None and result.confidence < 0.6:
            return None
    except Exception as exc:  # noqa: BLE001 - best effort by design
        log.debug("Ollama re-score skipped: %s", exc)
    return result
