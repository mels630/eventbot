"""Deterministic, dependency-light deduplication helpers.

Two events are considered "the same" when their titles are fuzzily similar,
their venues match, and their date ranges overlap or sit right next to each
other. This catches:

  * the same event surfaced by multiple sources (differently worded titles,
    venue spellings, or extra decoration like "— Official Site" / "Tickets"),
  * multiday / ongoing events that would otherwise land as one row per day.

All functions here are pure (no DB, no I/O) so they are trivially unit-testable.
The DB-facing merge logic lives in ``agent.persist_recommendations``.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from rapidfuzz import fuzz

# --------------------------------------------------------------------------- #
# Tunable thresholds                                                          #
# --------------------------------------------------------------------------- #

TITLE_THRESHOLD = 85.0   # min token_set_ratio (0-100) for titles to be "the same"
VENUE_THRESHOLD = 80.0   # min token_set_ratio for venues to be "the same"
DATE_GAP_DAYS = 1        # spans this many days apart still count as adjacent

# Common filler words that carry no distinguishing signal for event titles.
_STOPWORDS = {
    "the", "a", "an", "at", "in", "on", "of", "and", "&", "to", "for", "with",
    "presents", "presenting", "featuring", "feat", "ft", "live", "tickets",
    "ticket", "official", "site", "event", "events", "show",
    # Generic event-type descriptors: they describe the *kind* of event, not its
    # identity, so they must not prop up a fuzzy match on their own (e.g. two
    # different "exhibition opening reception"s at one gallery on one night).
    "exhibition", "exhibit", "opening", "reception", "preview", "matinee",
    "concert", "performance", "gala", "party", "celebration", "reading",
    "signing", "talk", "conversation", "outdoor", "indoor", "venue",
}


def normalize(text: str | None) -> str:
    """Lowercase, strip punctuation, drop stopwords, collapse whitespace."""
    if not text:
        return ""
    lowered = re.sub(r"[^a-z0-9\s]+", " ", text.lower())
    tokens = [t for t in lowered.split() if t and t not in _STOPWORDS]
    return " ".join(tokens)


# Tokens that mark an intentionally *distinct* session of an otherwise
# identically-named event (a Spanish vs English session, a sing-along vs regular
# showing, an online vs in-person option). If two titles disagree on these, they
# are different events even when the rest of the title is identical.
_VARIANT_MARKERS = {
    "spanish", "english", "bilingual", "sing", "singalong", "virtual", "online",
}


def variant_markers(title: str | None) -> frozenset[str]:
    """Language/version markers present in a title (see ``_VARIANT_MARKERS``)."""
    tokens = set(normalize(title).split())
    return frozenset(tokens & _VARIANT_MARKERS)


def _strip_venue_tokens(title: str | None, *venues: str | None) -> str:
    """Normalized title with any venue tokens removed.

    Sources often repeat the venue inside the title ("Author Talk at Copperfield's
    Books"), which adds tokens that aren't present in a sibling listing that
    describes the same event differently ("Author Talk — Reading & Signing").
    Dropping venue tokens removes that asymmetry so the real event tokens line up.
    Falls back to the full normalized title if stripping would empty it.
    """
    tokens = normalize(title).split()
    venue_tokens: set[str] = set()
    for v in venues:
        venue_tokens.update(normalize(v).split())
    stripped = [t for t in tokens if t not in venue_tokens]
    return " ".join(stripped) if stripped else " ".join(tokens)


def title_similarity(
    a: str | None,
    b: str | None,
    venue_a: str | None = None,
    venue_b: str | None = None,
) -> float:
    """0-100 similarity between two titles, order- and decoration-insensitive.

    When venues are supplied, venue tokens are stripped from both titles first so
    that a title echoing its venue still matches a sibling that doesn't.
    """
    na = _strip_venue_tokens(a, venue_a, venue_b)
    nb = _strip_venue_tokens(b, venue_a, venue_b)
    if not na or not nb:
        return 0.0
    return float(fuzz.token_set_ratio(na, nb))


def venue_similarity(a: str | None, b: str | None) -> float:
    """0-100 similarity between two venues.

    Containment (one normalized venue fully inside the other, e.g. "Blue Moon"
    vs "Blue Moon Tavern") is treated as a perfect match, since sources often
    abbreviate or qualify the same place.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        # If neither side names a venue we can't use it to distinguish; be
        # permissive so title + date can still decide.
        return 100.0 if na == nb else 0.0
    if na in nb or nb in na:
        return 100.0
    return float(fuzz.token_set_ratio(na, nb))


def date_ranges_related(
    start_a: datetime | None,
    end_a: datetime | None,
    start_b: datetime | None,
    end_b: datetime | None,
    gap_days: int = DATE_GAP_DAYS,
) -> bool:
    """True if [start_a, end_a] and [start_b, end_b] overlap or are within
    ``gap_days`` of each other. A missing end defaults to its start."""
    if start_a is None or start_b is None:
        # No usable dates: don't let dates veto a strong title+venue match.
        return True
    ea = end_a or start_a
    eb = end_b or start_b
    # Normalize ordering defensively.
    lo_a, hi_a = min(start_a, ea), max(start_a, ea)
    lo_b, hi_b = min(start_b, eb), max(start_b, eb)
    gap = timedelta(days=gap_days)
    # Ranges are "related" if, once padded by the gap, they intersect.
    return lo_a - gap <= hi_b and lo_b - gap <= hi_a


def is_same_event(
    title_a: str | None,
    venue_a: str | None,
    start_a: datetime | None,
    end_a: datetime | None,
    title_b: str | None,
    venue_b: str | None,
    start_b: datetime | None,
    end_b: datetime | None,
    *,
    title_threshold: float = TITLE_THRESHOLD,
    venue_threshold: float = VENUE_THRESHOLD,
    gap_days: int = DATE_GAP_DAYS,
) -> bool:
    """Two events match only when title AND venue AND dates all agree.

    Requiring all three keeps a single strong signal (e.g. a shared venue)
    from merging genuinely different events.
    """
    # Distinct language/version sessions (Spanish vs English, sing-along vs
    # regular) are different events even when the rest of the title matches.
    if variant_markers(title_a) != variant_markers(title_b):
        return False
    if title_similarity(title_a, title_b, venue_a, venue_b) < title_threshold:
        return False
    if venue_similarity(venue_a, venue_b) < venue_threshold:
        return False
    return date_ranges_related(start_a, end_a, start_b, end_b, gap_days)


def merge_span(
    start_a: datetime | None,
    end_a: datetime | None,
    start_b: datetime | None,
    end_b: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """Combine two date ranges into the widest span covering both."""
    starts = [d for d in (start_a, start_b) if d is not None]
    ends = [d for d in (end_a or start_a, end_b or start_b) if d is not None]
    new_start = min(starts) if starts else None
    new_end = max(ends) if ends else None
    # Collapse a degenerate span (end == start) back to "no end".
    if new_end is not None and new_start is not None and new_end <= new_start:
        new_end = None
    return new_start, new_end


def prefer_richer(current: str | None, candidate: str | None) -> str | None:
    """Pick the more informative of two text values (longer, non-empty wins)."""
    cur = (current or "").strip()
    cand = (candidate or "").strip()
    if len(cand) > len(cur):
        return cand
    return current


def cluster_duplicates(
    items: list[tuple[str | None, str | None, datetime | None, datetime | None]],
) -> list[list[int]]:
    """Group event descriptors that refer to the same event.

    ``items`` is a list of ``(title, venue, start, end)`` tuples. Returns a list
    of clusters, each a list of indices into ``items``. Matching is transitive
    (union-find), so consecutive-day listings of one festival chain into a single
    cluster. Singletons are included as one-element clusters.
    """
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i in range(n):
        ti, vi, si, ei = items[i]
        for j in range(i + 1, n):
            tj, vj, sj, ej = items[j]
            if is_same_event(ti, vi, si, ei, tj, vj, sj, ej):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())
