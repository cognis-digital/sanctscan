"""Core screening engine for SANCTSCAN.

Deterministic, explainable name screening against a sanctions watchlist.

Matching model (all standard library, fully deterministic):

* Names are normalized: unicode-decomposed to ASCII, lowercased, punctuation
  stripped, common corporate/honorific noise tokens removed.
* A token-set similarity is computed: each query token is greedily matched to
  its best watchlist token via a normalized edit-distance (Levenshtein ratio),
  which tolerates typos and transliteration drift.
* The aggregate score is the mean best-token ratio weighted by token coverage,
  with a sequential-difflib ratio blended in to reward overall string shape.
* Every hit carries a human-readable explanation listing which query token
  matched which watchlist token and at what ratio -- so a compliance analyst
  (or auditor) can see exactly *why* a name was flagged.
"""
from __future__ import annotations

import csv
import difflib
import json
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Tokens that carry no identifying signal and inflate/deflate scores.
_NOISE_TOKENS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "the", "of", "and", "a", "an",
        "co", "company", "corp", "corporation", "inc", "incorporated",
        "ltd", "limited", "llc", "plc", "sa", "ag", "gmbh", "bv",
        "group", "holdings", "holding", "international", "intl",
    }
)


@dataclass
class WatchlistEntry:
    """A single sanctions/watchlist record."""

    uid: str
    name: str
    program: str = ""
    entity_type: str = ""
    aliases: List[str] = field(default_factory=list)

    def all_names(self) -> List[str]:
        names = [self.name] + list(self.aliases)
        return [n for n in names if n and n.strip()]


@dataclass
class Hit:
    """An explainable screening hit."""

    query: str
    uid: str
    entity_name: str
    matched_name: str  # the watchlist name/alias that scored best
    score: float
    program: str
    entity_type: str
    explanation: str

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_name(name: str) -> str:
    """Normalize a name for comparison.

    Unicode-decompose to strip accents, lowercase, replace punctuation with
    spaces, and collapse whitespace. Deterministic and lossless of ordering.
    """
    if not name:
        return ""
    # Decompose accents (e.g. "é" -> "e") then drop combining marks.
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_bytes = decomposed.encode("ascii", "ignore")
    text = ascii_bytes.decode("ascii").lower()
    out_chars = []
    for ch in text:
        if ch.isalnum() or ch.isspace():
            out_chars.append(ch)
        else:
            out_chars.append(" ")
    return " ".join("".join(out_chars).split())


def tokenize(name: str, drop_noise: bool = True) -> List[str]:
    """Split a normalized name into tokens, optionally dropping noise tokens."""
    tokens = normalize_name(name).split()
    if drop_noise:
        filtered = [t for t in tokens if t not in _NOISE_TOKENS]
        # Never return empty if dropping noise removed everything.
        if filtered:
            return filtered
    return tokens


def _ratio(a: str, b: str) -> float:
    """Normalized similarity ratio in [0, 1] between two strings."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _token_set_score(
    q_tokens: Sequence[str], w_tokens: Sequence[str]
) -> Tuple[float, List[Tuple[str, str, float]]]:
    """Greedy best-match token-set similarity with per-token evidence.

    Returns (score, matches) where matches is a list of
    (query_token, watchlist_token, ratio).
    """
    if not q_tokens or not w_tokens:
        return 0.0, []

    matches: List[Tuple[str, str, float]] = []
    total = 0.0
    for qt in q_tokens:
        best_w = ""
        best_r = 0.0
        for wt in w_tokens:
            r = _ratio(qt, wt)
            if r > best_r:
                best_r = r
                best_w = wt
        matches.append((qt, best_w, round(best_r, 4)))
        total += best_r

    mean_best = total / len(q_tokens)

    # Coverage: fraction of query tokens that found a strong (>=0.85) partner.
    strong = sum(1 for _, _, r in matches if r >= 0.85)
    coverage = strong / len(q_tokens)

    # Blend mean-best with coverage so a single strong token can't carry a
    # multi-token query, but a fully-covered query is rewarded.
    score = 0.7 * mean_best + 0.3 * coverage
    return score, matches


def name_similarity(query: str, target: str) -> Tuple[float, str]:
    """Compute an explainable similarity score in [0, 1] between two names.

    Returns (score, explanation).
    """
    q_tokens = tokenize(query)
    w_tokens = tokenize(target)

    token_score, matches = _token_set_score(q_tokens, w_tokens)

    # Sequential whole-string ratio on normalized forms rewards overall shape
    # and order; blend it in lightly.
    seq = _ratio(normalize_name(query), normalize_name(target))
    score = round(0.8 * token_score + 0.2 * seq, 4)

    parts = []
    for qt, wt, r in matches:
        if wt:
            parts.append("%s~%s=%.2f" % (qt, wt, r))
        else:
            parts.append("%s~(none)" % qt)
    explanation = (
        "tokens[" + ", ".join(parts) + "] seq=%.2f -> %.2f" % (seq, score)
    )
    return score, explanation


def load_watchlist(path: str) -> List[WatchlistEntry]:
    """Load a watchlist from CSV or JSON.

    CSV columns recognized (case-insensitive, extras ignored):
        uid, name, program, type/entity_type, aliases ("; "-separated)
    JSON: a list of objects with the same keys, or {"entries": [...]}.
    """
    lower = path.lower()
    if lower.endswith(".json"):
        return _load_watchlist_json(path)
    return _load_watchlist_csv(path)


def _split_aliases(raw: str) -> List[str]:
    if not raw:
        return []
    out: List[str] = []
    for sep in (";", "|"):
        if sep in raw:
            return [a.strip() for a in raw.split(sep) if a.strip()]
    return [raw.strip()] if raw.strip() else out


def _load_watchlist_csv(path: str) -> List[WatchlistEntry]:
    entries: List[WatchlistEntry] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return entries
        # Map headers case-insensitively.
        cols = {c.lower().strip(): c for c in reader.fieldnames}
        for i, row in enumerate(reader):
            def get(key: str, default: str = "") -> str:
                col = cols.get(key)
                return (row.get(col) or default).strip() if col else default

            name = get("name")
            if not name:
                continue
            uid = get("uid") or get("id") or ("row-%d" % (i + 1))
            entity_type = get("entity_type") or get("type")
            entries.append(
                WatchlistEntry(
                    uid=uid,
                    name=name,
                    program=get("program"),
                    entity_type=entity_type,
                    aliases=_split_aliases(get("aliases") or get("alias")),
                )
            )
    return entries


def _load_watchlist_json(path: str) -> List[WatchlistEntry]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("entries", [])
    entries: List[WatchlistEntry] = []
    for i, obj in enumerate(data):
        name = (obj.get("name") or "").strip()
        if not name:
            continue
        aliases = obj.get("aliases") or []
        if isinstance(aliases, str):
            aliases = _split_aliases(aliases)
        entries.append(
            WatchlistEntry(
                uid=str(obj.get("uid") or obj.get("id") or ("row-%d" % (i + 1))),
                name=name,
                program=str(obj.get("program") or ""),
                entity_type=str(obj.get("entity_type") or obj.get("type") or ""),
                aliases=[str(a).strip() for a in aliases if str(a).strip()],
            )
        )
    return entries


def screen_name(
    query: str,
    watchlist: Iterable[WatchlistEntry],
    threshold: float = 0.80,
    max_hits: Optional[int] = None,
) -> List[Hit]:
    """Screen a single name against the watchlist.

    Returns hits at or above ``threshold``, sorted by descending score then
    uid (deterministic tie-break). For each entry, the best-scoring of its
    primary name and aliases is reported.
    """
    hits: List[Hit] = []
    for entry in watchlist:
        best_score = -1.0
        best_name = ""
        best_expl = ""
        for cand in entry.all_names():
            score, expl = name_similarity(query, cand)
            if score > best_score:
                best_score = score
                best_name = cand
                best_expl = expl
        if best_score >= threshold:
            hits.append(
                Hit(
                    query=query,
                    uid=entry.uid,
                    entity_name=entry.name,
                    matched_name=best_name,
                    score=round(best_score, 4),
                    program=entry.program,
                    entity_type=entry.entity_type,
                    explanation=best_expl,
                )
            )
    hits.sort(key=lambda h: (-h.score, h.uid))
    if max_hits is not None:
        hits = hits[:max_hits]
    return hits


def screen_records(
    names: Iterable[str],
    watchlist: Sequence[WatchlistEntry],
    threshold: float = 0.80,
    max_hits_per_name: Optional[int] = None,
) -> Dict[str, List[Hit]]:
    """Screen many names; return an ordered mapping name -> hits.

    Names with no hits are still present (empty list) so callers get a complete
    audit record of everything screened.
    """
    results: Dict[str, List[Hit]] = {}
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        results[name] = screen_name(
            name, watchlist, threshold=threshold, max_hits=max_hits_per_name
        )
    return results
