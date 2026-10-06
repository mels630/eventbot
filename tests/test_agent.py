import pytest
from eventbot.agent import EventCandidate, _title_slug, _query_hash


def test_event_candidate_from_dict():
    d = {
        "title": "Jazz Night",
        "venue": "Blue Moon Tavern",
        "event_date": "2026-05-15",
        "url": "https://example.com/jazz",
        "description": "A great jazz evening.",
        "score": 0.9,
        "relevance_notes": "Matches jazz interest",
    }
    c = EventCandidate.from_dict(d)
    assert c.title == "Jazz Night"
    assert c.score == 0.9


def test_event_candidate_defaults():
    c = EventCandidate.from_dict({"title": "X", "venue": "Y", "event_date": "2026-05-15", "url": "http://x.com"})
    assert c.score == 0.5
    assert c.description == ""
    assert c.end_date == ""


def test_event_candidate_parses_end_date():
    c = EventCandidate.from_dict({
        "title": "Art Fest",
        "venue": "Downtown",
        "event_date": "2026-08-01",
        "end_date": "2026-08-05",
        "url": "http://x.com",
    })
    assert c.end_date == "2026-08-05"


def test_title_slug_normalizes():
    assert _title_slug("Jazz Night at Blue Moon!") == "jazz-night-at-blue-moon"
    assert _title_slug("  Hello   World  ") == "hello-world"


def test_title_slug_truncates():
    long_title = "A" * 200
    assert len(_title_slug(long_title)) <= 80


def test_query_hash_deterministic():
    h1 = _query_hash("jazz events Portland this weekend")
    h2 = _query_hash("jazz events Portland this weekend")
    assert h1 == h2
    assert len(h1) == 16


def test_query_hash_case_insensitive():
    assert _query_hash("Jazz Events") == _query_hash("jazz events")
