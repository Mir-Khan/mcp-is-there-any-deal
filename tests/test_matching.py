"""Tests for title matching, mainly against real ITAD search results saved in fixtures/itad_search.json."""

import json
from pathlib import Path

import pytest

from deals_mcp.matching import match_level, normalize, rank_results, strip_suffix

SEARCHES = json.loads((Path(__file__).parent / "fixtures" / "itad_search.json").read_text(encoding="utf-8"))


def ranked_titles(query, n=None):
    return [(r["title"], level) for r, level in rank_results(query, SEARCHES[query])][:n]


# ---- normalize ----


@pytest.mark.parametrize(
    "a, b",
    [
        ("Final Fantasy 7", "FINAL FANTASY VII"),
        ("final fantasy xiv", "FINAL FANTASY 14"),
        ("Pokémon", "Pokemon"),
        ("The Witcher® 3™", "the witcher 3"),
        ("Tom Clancy's Rainbow Six", "Tom Clancys Rainbow Six"),
        ("Assassin’s Creed", "assassins creed"),
        ("Command & Conquer", "Command and Conquer"),
        ("  DOOM   Eternal ", "doom eternal"),
        ("Nier: Automata� Game", "nier automata game"),
    ],
)
def test_normalize_treats_these_as_equal(a, b):
    assert normalize(a) == normalize(b)


def test_letters_from_every_script_are_kept():
    assert normalize("原神") == "原神"
    assert normalize("Смута: Издание") == "смута издание"
    assert normalize("Ōkami HD") == "okami hd"
    assert match_level("Смута", "СМУТА") == "exact"
    assert match_level("原神", "Honkai") == "weak"


def test_roman_numerals_are_only_converted_as_whole_words():
    assert normalize("Final Fantasy VII") == "final fantasy 7"
    assert normalize("Mix Civ Vivid") == "mix civ vivid"
    assert normalize("XCOM 2") == "xcom 2"


def test_numerals_above_thirty_are_left_alone():
    # "XL" or "L" are far more likely to be words or sizes than sequel numbers.
    assert normalize("Final Fantasy XL") == "final fantasy xl"


def test_strip_suffix():
    assert strip_suffix("Resident Evil 4 (2005)") == "Resident Evil 4"
    assert strip_suffix("Final Fantasy III (3D Remake) (Steam)") == "Final Fantasy III"
    assert strip_suffix("Resident Evil 4 (2023) - Gold Edition") == "Resident Evil 4 (2023) - Gold Edition"


# ---- match_level ----


@pytest.mark.parametrize(
    "query, title, expected",
    [
        ("Final Fantasy", "FINAL FANTASY", "exact"),
        ("final fantasy 7", "FINAL FANTASY VII", "exact"),
        ("spiderman", "Spider-Man", "exact"),
        ("Resident Evil 4", "Resident Evil 4 (2005)", "variant"),
        ("Final Fantasy", "FINAL FANTASY XV WINDOWS EDITION", "starts_with"),
        # A query for VII must not count as the start of VIII, since VII and VIII are different words.
        ("Final Fantasy VII", "FINAL FANTASY VIII", "weak"),
        # Single-letter numerals are often part of a name, so a match through them is only a variant.
        ("Mega Man 10", "Mega Man X", "variant"),
        ("Final Fantasy 10", "FINAL FANTASY X", "variant"),
        ("Final Fantasy X", "FINAL FANTASY X", "exact"),
        ("Rainbow Six 5", "Rainbow Six V", "variant"),
        ("final fantasy 13", "FINAL FANTASY XIII", "exact"),
        ("Witcher 3", "The Witcher 3: Wild Hunt", "contains"),
        ("Final Fantasy", "LIGHTNING RETURNS: FINAL FANTASY XIII", "contains"),
        ("xyzzy", "Tomb Raider", "weak"),
    ],
)
def test_match_level(query, title, expected):
    assert match_level(query, title) == expected


# ---- rank_results on real search results ----


def test_final_fantasy_ranks_the_original_game_first():
    # ITAD itself puts the original game at position 24, behind XV and the VII remake.
    raw_titles = [r["title"] for r in SEARCHES["Final Fantasy"]]
    assert raw_titles.index("FINAL FANTASY") > 20

    titles = ranked_titles("Final Fantasy")
    assert titles[0] == ("FINAL FANTASY", "exact")
    assert sum(1 for _, level in titles if level == "exact") == 1


def test_numbered_sequel_finds_the_right_entry():
    titles = ranked_titles("final fantasy 7")
    assert titles[0] == ("FINAL FANTASY VII", "exact")
    assert ("FINAL FANTASY VIII", "exact") not in titles


def test_doom_prefers_the_exact_title_over_sequels():
    assert ranked_titles("Doom", 2) == [("DOOM", "exact"), ("DOOM Eternal", "starts_with")]


def test_remakes_sharing_a_name_are_both_variants():
    titles = ranked_titles("Resident Evil 4", 2)
    assert {t for t, _ in titles} == {"Resident Evil 4 (2005)", "Resident Evil 4 (2023)"}
    assert all(level == "variant" for _, level in titles)


def test_franchise_without_an_exact_title_has_no_exact_match():
    titles = ranked_titles("Halo")
    assert not any(level in ("exact", "variant") for _, level in titles)
    assert titles[0][1] == "starts_with"


def test_base_games_rank_above_dlc_and_soundtracks():
    results = rank_results("Celeste", SEARCHES["Celeste"])
    assert results[0][0]["title"] == "Celeste"
    starts_with = [r for r, level in results if level == "starts_with"]
    types = [r["type"] for r in starts_with]
    # Within one match level, every game comes before every DLC or untyped entry.
    assert types == sorted(types, key=lambda t: {"game": 0, "package": 1, "dlc": 2}.get(t, 3))


def test_subtitle_in_the_middle_is_a_contains_match():
    assert ranked_titles("Witcher 3", 1) == [("The Witcher 3: Wild Hunt", "contains")]


def test_nonsense_query_only_produces_weak_matches():
    assert all(level == "weak" for _, level in ranked_titles("xyzzy nonexistent game"))


def test_ranking_keeps_itad_order_within_a_level():
    results = [
        {"title": "Doom B", "type": "game"},
        {"title": "Doom A", "type": "game"},
    ]
    assert [r["title"] for r, _ in rank_results("doom", results)] == ["Doom B", "Doom A"]


def test_empty_results():
    assert rank_results("anything", []) == []
