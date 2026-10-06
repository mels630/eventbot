from datetime import datetime

import pytz

from eventbot import dedup

TZ = pytz.timezone("America/Los_Angeles")


def _dt(y, m, d):
    return TZ.localize(datetime(y, m, d))


def test_normalize_drops_punctuation_and_stopwords():
    assert dedup.normalize("Jazz Night at the Blue Moon!") == "jazz night blue moon"
    assert dedup.normalize("  Hello   World  ") == "hello world"
    assert dedup.normalize(None) == ""


def test_title_similarity_high_for_reordered_and_decorated():
    a = "Jazz Night at Blue Moon"
    b = "Blue Moon Presents: Jazz Night — Tickets"
    assert dedup.title_similarity(a, b) >= dedup.TITLE_THRESHOLD


def test_title_similarity_low_for_different_events():
    assert dedup.title_similarity("Jazz Night", "Punk Rock Show") < dedup.TITLE_THRESHOLD


def test_venue_similarity_containment_is_perfect():
    assert dedup.venue_similarity("Blue Moon", "Blue Moon Tavern") == 100.0


def test_venue_similarity_empty_is_permissive():
    assert dedup.venue_similarity("", "") == 100.0
    assert dedup.venue_similarity("Blue Moon", "") == 0.0


def test_date_ranges_overlap_and_adjacency():
    # Overlapping spans
    assert dedup.date_ranges_related(_dt(2026, 8, 1), _dt(2026, 8, 5), _dt(2026, 8, 3), None)
    # Adjacent within gap
    assert dedup.date_ranges_related(_dt(2026, 8, 1), None, _dt(2026, 8, 2), None)
    # Far apart -> not related
    assert not dedup.date_ranges_related(_dt(2026, 8, 1), None, _dt(2026, 8, 20), None)


def test_date_ranges_missing_dates_do_not_veto():
    assert dedup.date_ranges_related(None, None, _dt(2026, 8, 1), None)


def test_title_similarity_strips_venue_from_title():
    # Title A echoes the venue; title B describes the event differently.
    a = "Robert Hass & Brenda Hillman at Copperfield's Books"
    b = "Robert Hass & Brenda Hillman – Poetry Reading & Signing"
    va = "Copperfield's Books, 138 N Main St, Sebastopol, CA"
    vb = "Copperfield's Books, Sebastopol"
    # Stripping the venue tokens the title echoes lets the real event tokens
    # line up above the threshold.
    assert dedup.title_similarity(a, b, va, vb) >= dedup.TITLE_THRESHOLD


def test_is_same_event_venue_echoed_in_title():
    assert dedup.is_same_event(
        "Robert Hass & Brenda Hillman at Copperfield's Books",
        "Copperfield's Books, 138 N Main St, Sebastopol, CA",
        _dt(2026, 10, 2), None,
        "Robert Hass & Brenda Hillman – Poetry Reading & Signing",
        "Copperfield's Books, Sebastopol",
        _dt(2026, 10, 2), None,
    )


def test_venue_stripping_does_not_overmerge_distinct_events():
    # Two different events at the same venue/date must still stay separate.
    assert not dedup.is_same_event(
        "Kristin Hannah In Conversation",
        "Luther Burbank Center for the Arts",
        _dt(2026, 9, 26), None,
        "Robert Hass & Brenda Hillman Poetry Reading",
        "Luther Burbank Center for the Arts",
        _dt(2026, 9, 26), None,
    )


def test_generic_descriptors_do_not_merge_distinct_exhibitions():
    # Two different exhibitions sharing an opening-reception night at one gallery
    # must stay separate — generic type words ("exhibition opening reception")
    # should not carry the match.
    assert not dedup.is_same_event(
        "Under Many Moons - Special Exhibition Opening Reception at SebArts",
        "Sebastopol Center for the Arts",
        _dt(2026, 9, 27), None,
        "Member Exhibition Opening Reception - Sebastopol Center for the Arts",
        "Sebastopol Center for the Arts",
        _dt(2026, 9, 27), None,
    )


def test_variant_markers_block_language_sessions():
    # Spanish and English sessions of the same program are different events.
    assert not dedup.is_same_event(
        "Community Planning Dinner and Discussion; presented in Spanish",
        "City Hall", _dt(2026, 11, 4), None,
        "Community Planning Dinner and Discussion: presented in English",
        "City Hall", _dt(2026, 11, 4), None,
    )
    # Two Spanish sessions still merge.
    assert dedup.is_same_event(
        "Community Planning Dinner and Discussion; presented in Spanish",
        "City Hall", _dt(2026, 11, 4), None,
        "Community Planning Dinner and Discussion in Spanish",
        "City Hall", _dt(2026, 11, 4), None,
    )


def test_variant_markers_block_singalong():
    assert not dedup.is_same_event(
        "Disney's Frozen (Sing-Along) - SRJC Theatre Arts",
        "SRJC", _dt(2026, 11, 14), None,
        "Disney's Frozen - SRJC Theatre Arts",
        "SRJC", _dt(2026, 11, 14), None,
    )


def test_is_same_event_true_multi_source():
    assert dedup.is_same_event(
        "Jazz Night at Blue Moon", "Blue Moon", _dt(2026, 8, 1), None,
        "Blue Moon Presents Jazz Night Tickets", "Blue Moon Tavern", _dt(2026, 8, 1), None,
    )


def test_is_same_event_false_same_venue_different_event():
    assert not dedup.is_same_event(
        "Jazz Night", "Blue Moon", _dt(2026, 8, 1), None,
        "Comedy Open Mic", "Blue Moon", _dt(2026, 8, 1), None,
    )


def test_is_same_event_false_when_dates_far_apart():
    assert not dedup.is_same_event(
        "Jazz Night at Blue Moon", "Blue Moon", _dt(2026, 8, 1), None,
        "Jazz Night at Blue Moon", "Blue Moon", _dt(2026, 9, 1), None,
    )


def test_merge_span_widens():
    s, e = dedup.merge_span(_dt(2026, 8, 3), None, _dt(2026, 8, 1), _dt(2026, 8, 5))
    assert s == _dt(2026, 8, 1)
    assert e == _dt(2026, 8, 5)


def test_merge_span_collapses_single_day():
    s, e = dedup.merge_span(_dt(2026, 8, 1), None, _dt(2026, 8, 1), None)
    assert s == _dt(2026, 8, 1)
    assert e is None


def test_prefer_richer_picks_longer():
    assert dedup.prefer_richer("", "hello") == "hello"
    assert dedup.prefer_richer("a longer description", "short") == "a longer description"


def test_cluster_duplicates_groups_and_separates():
    items = [
        ("Jazz Night at Blue Moon", "Blue Moon", _dt(2026, 8, 1), None),
        ("Blue Moon Presents Jazz Night Tickets", "Blue Moon Tavern", _dt(2026, 8, 1), None),
        ("Comedy Open Mic", "Blue Moon", _dt(2026, 8, 1), None),
    ]
    clusters = dedup.cluster_duplicates(items)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 2]  # jazz pair merges, comedy stays alone


def test_cluster_duplicates_chains_consecutive_days():
    items = [
        ("Summer Art Festival", "City Park", _dt(2026, 8, 1), None),
        ("Summer Art Festival", "City Park", _dt(2026, 8, 2), None),
        ("Summer Art Festival", "City Park", _dt(2026, 8, 3), None),
    ]
    clusters = dedup.cluster_duplicates(items)
    assert len(clusters) == 1
    assert len(clusters[0]) == 3
