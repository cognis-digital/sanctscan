"""Smoke tests for SANCTSCAN.

Import the real engine, run it against the shipped demo files, and assert real
behavior (typo tolerance, alias matching, clean-name pass-through, CLI exit
codes, JSON shape). No network access.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest


from sanctscan import (
    TOOL_NAME,
    TOOL_VERSION,
    load_watchlist,
    name_similarity,
    normalize_name,
    screen_name,
    screen_records,
    tokenize,
)
from sanctscan.cli import main

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(REPO_ROOT, "demos", "01-basic")
WATCHLIST = os.path.join(DEMO, "watchlist.csv")
CUSTOMERS = os.path.join(DEMO, "customers.csv")


def test_metadata():
    assert TOOL_NAME == "sanctscan"
    assert TOOL_VERSION.count(".") == 2


def test_normalize_strips_accents_and_punct():
    assert normalize_name("Bashar al-Assad!") == "bashar al assad"
    # Accent fold: e-acute -> e
    assert normalize_name("André") == "andre"


def test_tokenize_drops_noise():
    assert tokenize("Acme Logistics LLC") == ["acme", "logistics"]
    # If everything is noise, fall back to raw tokens.
    assert tokenize("LLC") == ["llc"]


def test_similarity_typo_high():
    score, expl = name_similarity("Vladmir Putin", "Vladimir Putin")
    assert score >= 0.85
    assert "putin" in expl


def test_similarity_distinct_low():
    score, _ = name_similarity("Jane Public", "Vladimir Putin")
    assert score < 0.5


def test_load_watchlist():
    wl = load_watchlist(WATCHLIST)
    assert len(wl) == 6
    putin = next(e for e in wl if e.uid == "SDN-001")
    assert putin.name == "Vladimir Putin"
    assert "V. Putin" in putin.aliases
    assert putin.program == "RUSSIA-EO14024"


def test_screen_name_typo_hits():
    wl = load_watchlist(WATCHLIST)
    hits = screen_name("Vladmir Putin", wl, threshold=0.80)
    assert hits, "expected a hit on a typo of a sanctioned name"
    assert hits[0].uid == "SDN-001"
    assert hits[0].score >= 0.80
    assert hits[0].explanation  # non-empty, auditable


def test_screen_clean_name_clears():
    wl = load_watchlist(WATCHLIST)
    hits = screen_name("Jane Q. Public", wl, threshold=0.80)
    assert hits == []


def test_alias_transliteration_match():
    wl = load_watchlist(WATCHLIST)
    # Matches via the alias "Pjotr Iljitsch" of SDN-005.
    hits = screen_name("Pjotr Iljitsch", wl, threshold=0.80)
    assert any(h.uid == "SDN-005" for h in hits)


def test_screen_records_complete_audit():
    wl = load_watchlist(WATCHLIST)
    results = screen_records(
        ["Vladmir Putin", "Totally Clean Person"], wl, threshold=0.85
    )
    assert set(results) == {"Vladmir Putin", "Totally Clean Person"}
    assert results["Vladmir Putin"]
    assert results["Totally Clean Person"] == []


def test_threshold_filters():
    wl = load_watchlist(WATCHLIST)
    loose = screen_name("Kim Jong", wl, threshold=0.50)
    strict = screen_name("Kim Jong", wl, threshold=0.99)
    assert len(loose) >= len(strict)


def test_cli_flagged_returns_nonzero(capsys):
    code = main(["screen", "-w", WATCHLIST, "-n", "Vladmir Putin"])
    out = capsys.readouterr().out
    assert code == 1  # flagged -> non-zero for CI gating
    assert "HIT" in out


def test_cli_clean_returns_zero(capsys):
    code = main(["screen", "-w", WATCHLIST, "-n", "Zzxq Nonexistent"])
    out = capsys.readouterr().out
    assert code == 0
    assert "CLEAR" in out


def test_cli_json_format(capsys):
    code = main(
        ["screen", "-w", WATCHLIST, "-i", CUSTOMERS, "-c", "full_name",
         "--format", "json", "--threshold", "0.85"]
    )
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["tool"] == "sanctscan"
    assert payload["screened"] == 6
    assert payload["flagged"] >= 1
    assert code == 1
    # Every screened name present in results.
    queries = {r["query"] for r in payload["results"]}
    assert "Jane Q. Public" in queries


def test_cli_bad_threshold():
    assert main(["screen", "-w", WATCHLIST, "-n", "x", "-t", "5"]) == 2


def test_module_entrypoint_version():
    proc = subprocess.run(
        [sys.executable, "-m", "sanctscan", "--version"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "sanctscan" in proc.stdout.lower()


# ---------------------------------------------------------------------------
# Hardening tests: error paths and edge cases
# ---------------------------------------------------------------------------


def test_load_watchlist_missing_file():
    """Missing watchlist file raises OSError (not a raw traceback)."""
    with pytest.raises(OSError):
        load_watchlist("does_not_exist_ever.csv")


def test_load_watchlist_unsupported_extension():
    """Unsupported extension raises ValueError with a clear message."""
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
        f.write(b"<root/>")
        tmp = f.name
    try:
        with pytest.raises(ValueError, match="unsupported"):
            load_watchlist(tmp)
    finally:
        os.unlink(tmp)


def test_load_watchlist_json_non_list():
    """JSON watchlist that is neither list nor dict raises ValueError."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump("just a string", f)
        tmp = f.name
    try:
        with pytest.raises(ValueError, match="list"):
            load_watchlist(tmp)
    finally:
        os.unlink(tmp)


def test_load_watchlist_json_non_dict_items():
    """JSON list containing non-dict items is skipped gracefully."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(
            [42, None, "string", {"uid": "X1", "name": "Valid Name"}], f
        )
        tmp = f.name
    try:
        entries = load_watchlist(tmp)
        assert len(entries) == 1
        assert entries[0].uid == "X1"
    finally:
        os.unlink(tmp)


def test_screen_name_invalid_threshold():
    """screen_name raises ValueError for threshold outside [0, 1]."""
    from sanctscan.core import screen_name, WatchlistEntry

    wl = [WatchlistEntry(uid="T1", name="Test")]
    with pytest.raises(ValueError, match="threshold"):
        screen_name("test", wl, threshold=1.5)
    with pytest.raises(ValueError, match="threshold"):
        screen_name("test", wl, threshold=-0.1)


def test_cli_negative_max_hits(capsys):
    """--max-hits with a negative value returns exit code 2."""
    code = main(["screen", "-w", WATCHLIST, "-n", "test", "--max-hits", "-1"])
    err = capsys.readouterr().err
    assert code == 2
    assert "max-hits" in err


def test_cli_missing_input_file(capsys):
    """--input pointing to a nonexistent file returns exit code 2."""
    code = main(
        ["screen", "-w", WATCHLIST, "-i", "does_not_exist.csv"]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "error" in err.lower()


def test_cli_malformed_json_watchlist(capsys):
    """Malformed JSON watchlist returns exit code 2 with an error message."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        f.write("{broken json [}")
        tmp = f.name
    try:
        code = main(["screen", "-w", tmp, "-n", "test"])
        err = capsys.readouterr().err
        assert code == 2
        assert "error" in err.lower()
    finally:
        os.unlink(tmp)
