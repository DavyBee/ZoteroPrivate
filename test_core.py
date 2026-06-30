"""
test_core.py — Characterization tests for the pure-logic cores.

This is the project's safety net: with no ongoing maintainer, these pin the
behavior that's easy to break silently — URL/DOI normalization, duplicate
detection, the metadata-source taxonomy, and the enrich loop's bookkeeping.
They use an in-memory / temp-file database and never touch the network or
Turso, so they run fast and offline:

    python3 -m pytest test_core.py

Add a case here whenever you fix a bug, so it can't come back.
"""

from __future__ import annotations

import os

import pytest

# Tests must never hit a real Turso instance — force the local-sqlite path.
os.environ.pop("TURSO_DATABASE_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)

import app_helpers
import database
import metadata
import zotero_client
from database import Database, normalize_url, _is_real_doi


SSRN = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id={}"


@pytest.fixture
def db(tmp_path):
    """A fresh, file-backed Database (so save()/load() work like production)."""
    return Database.load(str(tmp_path / "t.db"), str(tmp_path / "p.json"))


def add(db, url, **fields):
    """Add a paper and apply enrichment fields; return its canonical key."""
    key, _ = db.add_paper(url)
    if fields:
        db.update_paper(key, **fields)
    return key


# ── normalize_url ─────────────────────────────────────────────────────────────

def test_normalize_url_preserves_query_so_distinct_ssrn_ids_stay_distinct():
    # Regression: three SSRN articles differ ONLY in the ?abstract_id= query.
    # If normalize_url dropped the query they'd collapse into one record.
    a, b, c = (normalize_url(SSRN.format(i)) for i in (4941708, 4554831, 4480068))
    assert a != b != c and a != c


def test_normalize_url_lowercases_scheme_host_but_keeps_path_case():
    assert normalize_url("HTTPS://Example.COM/Path/To") == "https://example.com/Path/To"


def test_normalize_url_strips_trailing_slash():
    assert normalize_url("https://example.com/x/") == "https://example.com/x"


# ── DOI validation / cleaning ───────────────────────────────────────────────────

@pytest.mark.parametrize("doi", ["10.2139/", "10.1093/", "10.2139", "", "not-a-doi"])
def test_bare_or_malformed_dois_are_rejected(doi):
    assert _is_real_doi(doi) is False
    assert metadata._clean_doi(doi) is None


@pytest.mark.parametrize("doi", ["10.2139/ssrn.4445621", "10.1093/sf/soab123"])
def test_real_dois_accepted(doi):
    assert _is_real_doi(doi) is True
    assert metadata._clean_doi(doi) == doi


def test_clean_doi_strips_trailing_punctuation_and_handles_none():
    assert metadata._clean_doi("10.1093/sf/soab123.") == "10.1093/sf/soab123"
    assert metadata._clean_doi(None) is None


# ── find_duplicate_groups ────────────────────────────────────────────────────────

def test_distinct_papers_with_bare_prefix_doi_do_not_group(db):
    # THE bug: SSRN/OUP can hand back a bare registrant prefix ("10.2139/") that
    # identifies the publisher, not the paper. Distinct works must NOT be merged.
    add(db, SSRN.format(4941708), doi="10.2139/", title="Watching the Watchdogs",
        metadata_source="translator")
    add(db, SSRN.format(4554831), doi="10.2139/", title="Something Entirely Different",
        metadata_source="translator")
    assert db.find_duplicate_groups() == []


def test_same_real_doi_groups_as_duplicate(db):
    add(db, "https://a.example/x", doi="10.1093/sf/soab123", title="Paper A",
        metadata_source="translator")
    add(db, "https://b.example/y", doi="10.1093/sf/soab123", title="Paper A (preprint)",
        metadata_source="crossref_doi")
    groups = db.find_duplicate_groups()
    assert len(groups) == 1 and len(groups[0]) == 2


def test_same_title_without_doi_groups(db):
    add(db, "https://a.example/x", title="Eviction and Children",
        metadata_source="llm")
    add(db, "https://b.example/y", title="eviction and children!",  # punct/case differ
        metadata_source="llm")
    groups = db.find_duplicate_groups()
    assert len(groups) == 1 and len(groups[0]) == 2


def test_tweets_are_never_duplicates(db):
    add(db, "https://x.com/user/status/111", title="t", metadata_source="link")
    add(db, "https://x.com/user/status/222", title="t", metadata_source="link")
    assert db.find_duplicate_groups() == []


# ── Metadata-source taxonomy (single source of truth in database.py) ────────────

def test_taxonomy_constants_are_consistent():
    assert set(database.READY_SOURCES) <= set(database.CITABLE_SOURCES)
    assert "llm" in database.CITABLE_SOURCES        # citable…
    assert "llm" not in database.READY_SOURCES      # …but not auto-approved


def test_uploadable_respects_taxonomy(db):
    add(db, "https://a.example/x", title="T", metadata_source="translator")
    add(db, "https://b.example/llm-unreviewed", title="T", metadata_source="llm")
    add(db, "https://c.example/llm-reviewed", title="T", metadata_source="llm",
        reviewed=True)
    urls = {p["url"] for p in zotero_client.uploadable_papers(db)}
    assert "https://a.example/x" in urls             # translator → ready
    assert "https://c.example/llm-reviewed" in urls  # reviewed llm → ready
    assert "https://b.example/llm-unreviewed" not in urls  # unreviewed llm → held back


def test_uploadable_does_not_itself_check_title(db):
    # Documents real behavior: uploadable_papers gates on source + reviewed only,
    # NOT title (its docstring's "have title" is guaranteed upstream by the
    # translator junk filter, not re-checked here). Pinned so a future refactor
    # that "tidies" the docstring doesn't accidentally change the gate.
    add(db, "https://d.example/no-title", metadata_source="translator")
    urls = {p["url"] for p in zotero_client.uploadable_papers(db)}
    assert "https://d.example/no-title" in urls


# ── Enrich loop orchestration (network stubbed) ─────────────────────────────────

def test_run_enrich_drives_every_pending_paper_once(db, monkeypatch):
    for i in range(3):
        db.add_paper(f"https://a.example/{i}")

    calls = []

    def fake_enrich_paper(paper, anthropic_key, audit_log_path):
        calls.append(paper["url"])
        return {"metadata_source": "translator", "title": "T", "attempts": 1}

    monkeypatch.setattr(app_helpers, "enrich_paper", fake_enrich_paper)
    steps = list(app_helpers.run_enrich(db, anthropic_key=None, audit_path="x"))

    assert len(calls) == 3                    # each pending paper enriched exactly once
    assert len(set(calls)) == 3               # no double-processing (the rerun-race bug)
    assert all(s["counts"]["ok"] <= 3 for s in steps)
    assert all(p["metadata_source"] == "translator" for p in db.all_papers())
