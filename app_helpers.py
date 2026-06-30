"""
app_helpers.py — UI orchestration helpers for the Streamlit app (app.py).

This module exists only to keep app.py under the 400-line cap mandated by
CLAUDE.md. It contains NO pipeline logic: every function here orchestrates UI
data or calls existing backend functions in database.py, slack_parser.py,
metadata.py, pdf_storage.py, zotero_client.py, and config.py.

Responsibilities:
  - status counting (internal metadata_source -> 5 user-facing states)
  - table-row builders for the Review tab (Title/Authors/Year/Journal/URL)
  - the synchronous enrich-loop runner (a generator yielding live progress)
  - ingest orchestration (mirrors cli.cmd_ingest)
  - upload-queue directory + count helpers
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Optional

import config
from database import (Database, hash_file, normalize_url,
                      READY_SOURCES, CITABLE_SOURCES)
from slack_parser import parse_files
from metadata import enrich_paper, fetch_webpage_metadata
from url_expander import is_tweet_url, expand_tweet


# ── UI-state mapping ────────────────────────────────────────────────────────
# The internal metadata_source taxonomy maps to the user-facing states shown in
# the status bar. A paper with a zotero_key is "Uploaded" regardless of source.
# See the table in CLAUDE.md's Streamlit app spec.

STATUS_ORDER = [
    "Pending", "Ready", "PDF only", "Review LLM", "Access issues",
    "Links", "Likely junk", "Upload error", "Uploaded",
]

_SOURCE_TO_STATE = {
    "none":         "Pending",
    "translator":   "Ready",
    "crossref_doi": "Ready",
    "pdf_saved":    "PDF only",
    "needs_manual": "Access issues",
    "link":         "Links",
    "non_paper":    "Likely junk",
}

# READY_SOURCES / CITABLE_SOURCES are imported from database (the single source
# of truth for the metadata-source taxonomy).


def ui_state(paper: dict) -> str:
    """Map one paper to its user-facing state."""
    if paper.get("zotero_key") or paper.get("pdf_confirmed") or paper.get("manually_added"):
        return "Uploaded"
    if paper.get("upload_error"):
        return "Upload error"      # Zotero rejected it — held out of Ready for fixing
    src = paper.get("metadata_source", "none")
    if src == "llm":
        # An LLM record is only Ready once a human has accepted it.
        return "Ready" if paper.get("reviewed") else "Review LLM"
    return _SOURCE_TO_STATE.get(src, "Pending")


def status_counts(db: Database) -> dict[str, int]:
    """Count papers by user-facing state, in STATUS_ORDER. Single source of truth
    for the status-bar breakdown, so each bucket here matches the paper list its
    Review tab shows."""
    counts = {s: 0 for s in STATUS_ORDER}
    for p in db.all_papers():
        counts[ui_state(p)] += 1
    return counts


# ── Database loading ────────────────────────────────────────────────────────

def load_db() -> Database:
    """Load the canonical DB from disk (the only place app.py reads from disk)."""
    return Database.load(str(config.DB_PATH), str(config.PROCESSED_FILES_PATH))


# ── Review-table builders ───────────────────────────────────────────────────
# Each builder returns a list of row dicts ready for st.dataframe. The "URL"
# column is a plain string rendered clickable via st.column_config.LinkColumn.
# Row order is preserved so the caller can map a selected row index back to its
# URL for deletion / reclassification.

def paper_rows(papers: list[dict]) -> list[dict]:
    """Title / Authors / Year / Journal / URL rows for a metadata table.
    Authors are the full "; "-joined list so the cells round-trip when edited."""
    rows = []
    for p in papers:
        rows.append({
            "Title":   p.get("title") or "",
            "Authors": "; ".join(p.get("authors") or []),
            "Year":    p.get("year") or "",
            "Journal": p.get("journal") or "",
            "DOI":     p.get("doi") or "",
            "URL":     p.get("url") or "",
        })
    return rows


def apply_table_edits(db: Database, papers: list[dict], edited_rows) -> int:
    """Write edited Title/Authors/Year/Journal cells back to the DB (by row
    order, which matches `papers`). Authors split on ';'. Returns rows changed.

    Called on every rerun for auto-save, so it only touches disk when something
    actually changed — an unchanged table is a cheap compare with no save."""
    n = 0
    for i, row in enumerate(edited_rows):
        if i >= len(papers):
            break
        p = papers[i]
        yr = row.get("Year")
        changed = {}
        new_title = (row.get("Title") or "").strip() or None
        new_authors = [a.strip() for a in (row.get("Authors") or "").split(";") if a.strip()]
        new_year = (str(yr).strip() or None) if yr not in (None, "") else None
        new_journal = (row.get("Journal") or "").strip() or None
        if new_title != p.get("title"):
            changed["title"] = new_title
        if new_authors != (p.get("authors") or []):
            changed["authors"] = new_authors
        if new_year != p.get("year"):
            changed["year"] = new_year
        if new_journal != p.get("journal"):
            changed["journal"] = new_journal
        if changed:
            db.update_paper(normalize_url(p["url"]), **changed)
            n += 1
    if n:
        db.save()
    return n


def link_papers(db: Database) -> list[dict]:
    """`link` items — reachable non-papers worth keeping as webpage bookmarks
    (the Links tab). Expanded-tweet wrappers are reclassified to `link` at
    expansion time, so they show up here as ordinary bookmarks like everything
    else (see expand_tweets)."""
    return papers_by_source(db, ("link",))


def junk_papers(db: Database) -> list[dict]:
    """`non_paper` items judged to be clutter (the Likely-junk tab)."""
    return papers_by_source(db, ("non_paper",))


def triage_papers(db: Database) -> list[dict]:
    """`needs_manual` items (the Access-issues tab)."""
    return papers_by_source(db, ("needs_manual",))


def _low_context_flag(p: dict) -> str:
    """'⚠' when the LLM had little/no page text for this paper (it guessed from
    the URL + Slack) — a cue to verify the metadata."""
    return "⚠" if p.get("low_context") else ""


def llm_rows(papers: list[dict]) -> list[dict]:
    """LLM-review rows: the standard metadata columns plus a leading low-context
    flag, so records the LLM guessed (no real page text) stand out for a check."""
    rows = []
    for p, base in zip(papers, paper_rows(papers)):
        rows.append({"⚠": _low_context_flag(p), **base})
    return rows


def triage_rows(papers: list[dict]) -> list[dict]:
    """needs_manual rows. Title/year are omitted — for blocked/unretrievable
    items they're almost never found. Instead lead with the LLM's one-line
    summary. No low-context flag: these were blocked/unretrievable so they're
    low-context by definition, and the flag only carries signal where we're
    accepting LLM-extracted metadata (the LLM tab)."""
    rows = []
    for p in papers:
        rows.append({
            "Access":  "⚠ Access issues",
            "Summary": p.get("summary") or "",
            "DOI":     p.get("doi") or "",
            "URL":     p.get("url") or "",
        })
    return rows


def cull_rows(papers: list[dict]) -> list[dict]:
    """Read-only view for the Links / Likely-junk tabs: the LLM's one-line
    summary (the at-a-glance culling aid) + any title + the clickable URL, in
    `papers` order so selection indices map back. No low-context flag — these
    are categorization buckets, not metadata to accept, so the flag (which only
    means "verify the LLM-extracted citation") would just be noise here."""
    rows = []
    for p in papers:
        rows.append({
            "Summary": p.get("summary") or "",
            "Title":   p.get("title") or "",
            "URL":     p.get("url") or "",
        })
    return rows


def delete_papers(db: Database, urls: list[str]) -> int:
    """Delete papers AND their downloaded PDFs (canonical store + upload queue).
    All deletions happen in memory first, then one save at the end. Returns count."""
    n = 0
    for url in urls:
        if db.delete_paper(url):
            n += 1
    if n:
        db.save()
    return n


def reset_papers(db: Database, urls: list[str]) -> int:
    """Reset papers to Pending so the next Enrich re-fetches from scratch.
    db.reset_paper deletes each paper's cached PDF. Returns the count reset;
    saves once at the end."""
    n = 0
    for url in urls:
        if db.reset_paper(url):
            n += 1
    db.save()
    return n


def papers_by_source(db: Database, sources: tuple[str, ...]) -> list[dict]:
    """Non-uploaded papers whose metadata_source is in `sources`. Excludes any
    that now count as Uploaded — incl. ones marked manually_added via the Access-
    issues connector flow — so confirmed papers leave their curation queue."""
    return [
        p for p in db.all_papers()
        if p.get("metadata_source") in sources and ui_state(p) != "Uploaded"
    ]


def upload_error_papers(db: Database) -> list[dict]:
    """Papers Zotero rejected on upload — held out of Ready until fixed + retried."""
    return [p for p in db.all_papers()
            if p.get("upload_error") and not p.get("zotero_key")]


def upload_error_rows(papers: list[dict]) -> list[dict]:
    """Rows for the Upload-tab fix list: the Zotero error message plus the same
    editable Title/Authors/Year/Journal cells as the review tables (so the same
    apply_table_edits writes them back), in `papers` order."""
    rows = []
    for p in papers:
        rows.append({
            "Error":   p.get("upload_error") or "",
            "Title":   p.get("title") or "",
            "Authors": "; ".join(p.get("authors") or []),
            "Year":    p.get("year") or "",
            "Journal": p.get("journal") or "",
            "URL":     p.get("url") or "",
        })
    return rows


def papers_by_upload_state(db: Database) -> tuple[list[dict], list[dict]]:
    """Split all papers into (uploaded, not_uploaded) by user-facing state."""
    up, not_up = [], []
    for p in db.all_papers():
        (up if ui_state(p) == "Uploaded" else not_up).append(p)
    return up, not_up


def full_rows(papers: list[dict]) -> list[dict]:
    """All view-only metadata for the Database tab, one row per paper."""
    rows = []
    for p in papers:
        rows.append({
            "State":     ui_state(p),
            "Title":     p.get("title") or "",
            "Authors":   "; ".join(p.get("authors") or []),
            "Year":      p.get("year") or "",
            "Journal":   p.get("journal") or "",
            "Type":      p.get("item_type") or "",
            "DOI":       p.get("doi") or "",
            "Source":    p.get("metadata_source") or "",
            "PDF":       "📄" if p.get("local_pdf_path") else "",
            "Comments":  len(p.get("comments") or []),
            "Zotero key": p.get("zotero_key") or "",
            "Added":     (p.get("first_seen_at") or "")[:10],
            "URL":       p.get("url") or "",
        })
    return rows


def llm_review_queue(db: Database) -> list[dict]:
    """LLM-sourced papers awaiting human acceptance (not reviewed, not uploaded)."""
    return [
        p for p in db.all_papers()
        if p.get("metadata_source") == "llm"
        and not p.get("reviewed") and not p.get("zotero_key")
    ]


# ── Link-upload metadata (Option A: let Zotero's translators title the page) ──

def resolve_link_metadata(url: str) -> dict:
    """What Zotero's own translator makes of an arbitrary URL, for link uploads.

    Calls Citoid (junk filter bypassed) — the same translator engine the browser
    connector uses. item_type is forced to "webpage" so the upload can never be
    rejected for an unsupported type; we keep the translator's title / website /
    date / authors. Falls back to a bare-URL title when Citoid returns nothing."""
    fields = fetch_webpage_metadata(url) or {}
    out: dict = {"item_type": "webpage", "title": fields.get("title") or url}
    for f in ("journal", "year", "abstract", "language", "authors"):
        if fields.get(f):
            out[f] = fields[f]
    return out


def apply_link_metadata(db: Database, papers: list[dict], progress_cb=None) -> None:
    """Resolve each link's metadata via Citoid and write it back, in place,
    before a link upload. progress_cb(done, total) if given."""
    total = len(papers)
    for i, p in enumerate(papers, 1):
        db.update_paper(normalize_url(p["url"]), **resolve_link_metadata(p["url"]))
        if progress_cb:
            progress_cb(i, total)


def promote_links_to_ready(db: Database, urls: list[str]) -> int:
    """Move selected URLs into the Ready bucket so they upload on the next run.

    Promotion is purely local — NO network lookup — so it's instant however many
    you select. A paper that already has a title keeps it; one without gets the
    URL as its title and item_type=webpage so it has something to show and can
    never be rejected for an unsupported type. (The upload path also coerces any
    unknown/missing type to webpage, so a plain link uploads fine.) Marking it
    `translator` is what makes it count as Ready. The operator's selection/edit
    is the review step — a nicer title can be set inline in Edit mode. Returns
    the count promoted; saves once at the end.
    """
    n = 0
    for url in urls:
        p = db.get_paper(url)
        if p is None:
            continue
        fields = {"metadata_source": "translator"}    # → Ready / uploadable
        if not p.get("title"):
            fields["title"] = url
            fields["item_type"] = "webpage"
        db.update_paper(normalize_url(url), **fields)
        n += 1
    db.save()
    return n


def accept_llm(db: Database, urls: list[str]) -> int:
    """Mark LLM-sourced records reviewed so they move from the LLM queue into
    Ready (an LLM record only uploads once a human has accepted it). The promote
    action behind the LLM tab's 'Save & move to Ready'. Returns count accepted."""
    n = 0
    for url in urls:
        if db.get_paper(url) is None:
            continue
        db.update_paper(normalize_url(url), reviewed=True)
        n += 1
    db.save()
    return n


# ── Tweet expansion (recover the paper a tweet links to) ──────────────────────

def unexpanded_tweet_count(db: Database) -> int:
    return sum(1 for p in db.all_papers()
               if is_tweet_url(p.get("url", "")) and not p.get("tweet_expanded"))


# A Links-tab entry is a plain URL bookmark with NO bibliographic metadata —
# that's the whole point of the tab. These are the fields blanked when something
# becomes a link (e.g. a tweet whose paper we've already pulled into its own
# record), so a bare link can never collide with a real paper in the Duplicates
# tab or masquerade as a citable work.
_LINK_STRIP_FIELDS = ("title", "doi", "journal", "authors", "year", "abstract",
                      "volume", "issue", "pages", "isbn", "publisher",
                      "language", "keywords")


def make_plain_link(db: Database, url: str) -> None:
    """Turn a paper into a plain link: blank its bibliographic metadata and mark
    it a `link` webpage. Used when a tweet is expanded (the tweet itself is just
    a pointer) and to keep the Links tab metadata-free."""
    fields = {f: None for f in _LINK_STRIP_FIELDS}
    fields["metadata_source"] = "link"
    fields["item_type"] = "webpage"
    db.update_paper(normalize_url(url), **fields)


def expand_tweets(db: Database, progress_cb=None) -> dict:
    """For every not-yet-expanded tweet URL, look up the link it shares and add
    that target as a new pending paper (carrying over the tweet's Slack
    comments). progress_cb(done, total) if given.
    Returns {scanned, links_found, new_papers, gone, failed}."""
    work = [p for p in db.all_papers()
            if is_tweet_url(p.get("url", "")) and not p.get("tweet_expanded")]
    total = len(work)
    scanned = links = new = gone = failed = 0
    for p in work:
        url = p["url"]
        scanned += 1
        status, target = expand_tweet(url)
        if status == "ok":
            links += 1
            key, is_new = db.add_paper(target)
            for c in p.get("comments", []) or []:
                db.add_comment(key, c)
            if is_new:
                new += 1
            make_plain_link(db, url)
            db.update_paper(normalize_url(url), tweet_expanded=True, tweet_target=target)
        elif status == "gone":
            # Permanently deleted or protected — confirmed dead, goes to Junk.
            gone += 1
            db.update_paper(normalize_url(url), tweet_expanded=True, tweet_target=None,
                            metadata_source="non_paper", item_type="webpage")
        elif status == "empty":
            # Tweet was readable but shares no external link — keep as a plain
            # link bookmark (it's a real tweet, just text/media only).
            make_plain_link(db, url)
            db.update_paper(normalize_url(url), tweet_expanded=True, tweet_target=None)
        else:
            # Transient error (X down, network failure) — keep as a plain link;
            # the tweet likely exists, we just couldn't read it right now.
            failed += 1
            make_plain_link(db, url)
            db.update_paper(normalize_url(url), tweet_expanded=True, tweet_target=None)
        if progress_cb:
            progress_cb(scanned, total)
    db.save()
    return {"scanned": scanned, "links_found": links,
            "new_papers": new, "gone": gone, "failed": failed}


# ── Debug-mode pipeline resets ────────────────────────────────────────────────
# The three most common resets (all-enrichment, uploads, ingest) get dedicated
# buttons in app.py. DEBUG_ACTIONS holds only the *other* resets, shown in a
# dropdown — no overlap with the buttons. Friendly label → internal action key.
DEBUG_ACTIONS = {
    "Reset Ready → Pending":                          "ready",
    "Reset PDF-only → Pending":                       "pdf",
    "Reset Review-LLM → Pending":                     "llm",
    "Reset Access issues → Pending":                  "triage",
    "Reset Links → Pending":                          "links",
    "Reset Likely-junk → Pending":                    "skipped",
    "Reset errored → Pending":                        "errored",
    "Clear tweet-expansion flags (re-expand tweets)": "tweets",
    "WIPE everything (delete all papers + state)":    "wipe_all",
}

_DEBUG_BUCKETS = {
    "ready":   set(READY_SOURCES),
    "pdf":     {"pdf_saved"},
    "llm":     {"llm"},
    "triage":  {"needs_manual"},
    "links":   {"link"},
    "skipped": {"non_paper"},
}


def debug_reset(db: Database, action: str) -> str:
    """Run one debug reset and save. Returns a human-readable result line."""
    # revert_to_baseline restores into `db` in place and saves itself, so it
    # returns before the db.save() at the end of this function.
    if action == "revert_review":
        if revert_to_baseline(db):
            return "Restored the post-enrichment state — all Review-tab changes undone."
        return "No baseline yet. Run Enrich once to create the post-enrichment checkpoint."

    if action == "enrich_all":
        n = db.reset_enrichment()
        msg = f"Reset {n} paper(s) to Pending."
    elif action in _DEBUG_BUCKETS:
        n = db.reset_enrichment(states=_DEBUG_BUCKETS[action])
        msg = f"Reset {n} paper(s) to Pending."
    elif action == "errored":
        n = db.reset_enrichment(states=set(), include_errored=True)
        msg = f"Reset {n} errored paper(s) to Pending."
    elif action == "uploaded":
        n = db.reset_uploads()
        msg = f"Cleared upload status from {n} paper(s)."
    elif action == "tweets":
        n = db.reset_tweet_expansion()
        msg = f"Cleared tweet-expansion flags on {n} tweet(s)."
    elif action == "processed_files":
        n = db.reset_processed_files()
        msg = f"Cleared {n} processed-file record(s); Slack files will re-ingest."
    elif action == "wipe_all":
        n = db.clear_all_papers()
        db.reset_processed_files()
        msg = f"Wiped everything: deleted {n} paper(s) and the processed-files registry."
    else:
        return "Unknown action."
    db.save()
    return msg


def open_urls(urls: list[str]) -> int:
    """Open each URL as a new browser tab via JavaScript window.open(), de-duped,
    blanks dropped. Works on both local and hosted (cloud) deployments — the JS
    runs in the user's browser, not on the server. Browsers may block popups if
    many tabs are opened at once; the user may need to allow them once. Returns
    count opened."""
    import json
    import streamlit.components.v1 as components
    seen: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.append(u)
    if not seen:
        return 0
    js = "".join(f"window.open({json.dumps(u)}, '_blank');" for u in seen)
    components.html(f"<script>{js}</script>", height=0)
    return len(seen)


# ── Claude model availability ─────────────────────────────────────────────────

def llm_model_status(api_key: Optional[str], model: str) -> str:
    """Is `model` still offered by the Anthropic API? Never raises. Returns:
        "ok"          — the model resolves (current)
        "unavailable" — the API returned 404: the model was deprecated/retired
                        (or the ID is a typo) and a new one must be chosen
        "no_key"      — no API key configured, so we can't check
        "uncheckable" — SDK missing, no models API, or a network/API error

    Uses models.retrieve (not list membership) so model *aliases* like
    'claude-sonnet-4-5' resolve correctly instead of false-flagging — the live
    model list only contains dated snapshots, but the API accepts both.
    """
    if not api_key:
        return "no_key"
    try:
        import anthropic
    except ImportError:
        return "uncheckable"
    try:
        anthropic.Anthropic(api_key=api_key).models.retrieve(model)
        return "ok"
    except Exception as e:
        if getattr(e, "status_code", None) == 404 or type(e).__name__ == "NotFoundError":
            return "unavailable"
        return "uncheckable"      # offline / auth / older SDK without models API


def llm_credit_status(api_key: Optional[str], model: str) -> str:
    """Whether the Anthropic account has credit for inference. Never raises:
        "ok"          — a minimal message succeeded
        "no_credit"   — the API returned the 'credit balance is too low' 400
        "no_key"      — no key configured
        "uncheckable" — SDK missing / network / other API error (a retired model
                        is the model-status banner's job, not this one)

    Costs one 1-token message per check (the caller caches it per session).
    Separate from llm_model_status because models.retrieve neither consumes nor
    reports credit — only an actual inference call does."""
    if not api_key:
        return "no_key"
    try:
        import anthropic
    except ImportError:
        return "uncheckable"
    try:
        anthropic.Anthropic(api_key=api_key).messages.create(
            model=model, max_tokens=1, messages=[{"role": "user", "content": "."}])
        return "ok"
    except Exception as e:
        msg = str(e).lower()
        if "credit balance" in msg or "too low" in msg:
            return "no_credit"
        return "uncheckable"


def available_models(api_key: Optional[str]) -> list[str]:
    """Current Claude model IDs offered by this key, for the Settings picker.
    Returns [] when it can't be determined. Never raises."""
    if not api_key:
        return []
    try:
        import anthropic
        ids = [m.id for m in anthropic.Anthropic(api_key=api_key).models.list(limit=1000).data]
    except Exception:
        return []
    claude = [m for m in ids if m.startswith("claude")]
    return claude or ids


# ── Enrich-loop runner ──────────────────────────────────────────────────────

def _outcome(fields: dict) -> tuple[str, str]:
    """Classify an enrich result into (category, human label).

    Categories tally into the run summary; labels feed the live log. Mirrors
    the bucketing logic in cli.cmd_enrich so GUI output matches the CLI.
    """
    src = fields.get("metadata_source")
    title = fields.get("title")
    pdf = " 📄" if fields.get("local_pdf_path") else ""

    if src == "pdf_saved":
        size_mb = (fields.get("pdf_size_bytes") or 0) / (1024 * 1024)
        return "pdf", f"📄 PDF saved ({size_mb:.1f} MB)"
    if src == "non_paper":
        return "non", "◦ likely junk"
    if src == "link":
        return "link", "◦ link"
    if src == "needs_manual":
        return "manual", "⚑ access issues"
    if src in READY_SOURCES and title:
        return "ok", f"✓ {src}{pdf}: {title[:60]}"
    if src in CITABLE_SOURCES and title:
        return "llm", f"✓ {src}{pdf}: {title[:60]}"
    if src in CITABLE_SOURCES:
        bits = []
        for f in ("doi", "year", "journal"):
            if fields.get(f):
                bits.append(f"{f}={fields[f]}")
        if fields.get("authors"):
            bits.append(f"{len(fields['authors'])} author(s)")
        return "llm", f"~ {src}{pdf} partial: {', '.join(bits) or 'minimal'}"
    err = fields.get("last_error") or "no metadata"
    return "fail", f"✗ {err}"


# The enrich run is driven one paper per Streamlit rerun. We deliberately hold
# only PLAIN DATA in session_state (a URL list + an index + counters), never a
# live generator: Streamlit reruns on every widget interaction, and a generator
# stored across reruns gets re-entered by an overlapping run → "ValueError:
# generator already executing", and a half-finished run could leave it wedged.
# With an index, an overlapping/abandoned rerun at worst re-does one paper (the
# skip-guard below usually avoids even that) — it never crashes and never stalls.

def enrich_init(db: Database) -> dict:
    """Snapshot the pending work as resumable session-state data."""
    return {
        "urls": [p["url"] for p in db.unenriched_papers()],
        "idx": 0,            # next paper to attempt
        "done": 0,           # papers actually enriched (for progress / counts)
        "counts": {"ok": 0, "llm": 0, "pdf": 0, "manual": 0,
                   "link": 0, "non": 0, "fail": 0,
                   # pdf_total counts every paper that got a PDF on disk this run
                   # (ready/llm with an attachment AND the PDF-only ones), so the
                   # summary can report total PDFs saved, not just the PDF-only bucket.
                   "pdf_total": 0},
    }


def enrich_step(db: Database, state: dict, anthropic_key: str | None,
                audit_path: str) -> dict:
    """Enrich the next pending paper, mutating `state` in place. Returns a step:
        done   → {"done": True,   "i", "total", "counts"}   (saved + baseline taken)
        skip   → {"skip": True,   ...}                       (gone / already done)
        normal → {"i","total","url","category","label","counts","done":False,...}

    Re-entrancy-safe: we CLAIM a paper (advance the index) before the slow
    enrich work, not after. enrich_paper makes no st.* calls, so Streamlit's
    cooperative "stop the old run" can't interrupt it — a rerun fired mid-call
    lets the first run finish in the background while the second starts. If the
    index only advanced afterwards, that second run would read the same index,
    see the paper still unenriched, and enrich it AGAIN: a duplicate LLM call
    (real cost) and a double count, while both writes land on one DB key so the
    library still shows one record. Claiming first shrinks the race window to a
    dict lookup with no I/O, so an overlapping rerun moves on to the next paper.
    A paper whose run is abandoned after the claim stays metadata_source=="none"
    and is simply picked up by the NEXT enrich run (enrich_init re-includes every
    unenriched paper) — deferred, never lost. The metadata_source=="none" guard
    still skips anything already done without re-counting."""
    urls = state["urls"]
    total = len(urls)
    i = state["idx"]

    if i >= total:
        db.save()
        snapshot_baseline(db)   # checkpoint post-enrich state for "Undo Review changes"
        return {"done": True, "i": state["done"], "total": total,
                "counts": dict(state["counts"])}

    url = urls[i]
    state["idx"] = i + 1       # claim this paper BEFORE the slow work (see docstring)
    paper = db.get_paper(url)
    if paper is None or paper.get("metadata_source", "none") != "none":
        return {"skip": True, "i": state["done"], "total": total, "url": url,
                "counts": dict(state["counts"]), "done": False, "halted": False}

    fields = enrich_paper(paper, anthropic_key, audit_path)
    db.update_paper(normalize_url(url), **fields)
    category, label = _outcome(fields)
    state["counts"][category] += 1
    if fields.get("local_pdf_path"):
        state["counts"]["pdf_total"] += 1
    state["done"] += 1
    if state["done"] % 10 == 0:
        db.save()
    return {"i": state["done"], "total": total, "url": url, "category": category,
            "label": label, "counts": dict(state["counts"]),
            "done": False, "halted": False}


def run_enrich(db: Database, anthropic_key: str | None,
               audit_path: str) -> Iterator[dict]:
    """Generator interface over enrich_init/enrich_step — used by tests and any
    non-Streamlit caller. The GUI does NOT use this (it drives enrich_step one
    paper per rerun); see the module note above on why no generator is stored."""
    state = enrich_init(db)
    while True:
        step = enrich_step(db, state, anthropic_key, audit_path)
        if step.get("done"):
            return
        if step.get("skip"):
            continue
        yield step


# ── Post-enrich baseline (powers the "Undo Review changes" reset) ─────────────
# The baseline is a copy of the DB taken right after enrichment — the state
# before any Review-tab curation. Restoring it undoes every Review-tab edit,
# delete, reclassification, and duplicate resolution.

def has_baseline(db: Database) -> bool:
    return db.has_named_snapshot("baseline")


def snapshot_baseline(db: Database) -> None:
    """Save the current state as the post-enrichment baseline (a durable snapshot
    row in the DB, so it works on Turso and survives restarts)."""
    db.save_named_snapshot("baseline")


def ensure_baseline(db: Database) -> None:
    """Create a baseline from the current DB if none exists yet, so an already-
    enriched DB loaded at startup still has a checkpoint to revert to."""
    if not db.has_named_snapshot("baseline"):
        db.save_named_snapshot("baseline")


def revert_to_baseline(db: Database) -> bool:
    """Restore the post-enrichment baseline into `db`, then clear every review
    acceptance. Returns False if no baseline exists.

    Clearing `reviewed` is essential: the baseline is snapshotted at enrich time,
    so a review-then-enrich sequence captures the acceptances *into* the baseline.
    Without this, restoring it wouldn't put accepted LLM items back in the review
    queue (they'd already be marked reviewed). Post-enrichment, nothing is
    reviewed, so dropping all acceptances is the correct "undo review" state.
    """
    if not db.restore_named_snapshot("baseline"):
        return False
    if any(p.get("reviewed") for p in db.all_papers()):
        for p in db.all_papers():
            p["reviewed"] = None
        db.save()
    return True


# ── Single-step undo (per-action, distinct from the post-enrich baseline) ─────
# Handled in app.py: an undoable action stashes db.snapshot_state() (an in-RAM
# copy) in session_state *before* it runs, and the Undo banner restores it via
# db.restore_state(). One level deep (the last action), this session only. Note:
# undoing a DELETE brings the records back, but a deleted paper's local PDF was
# already removed from disk — re-enrich re-fetches it.


# ── PDF export (driven by the DB, not the stale pdfs_to_upload/ cache) ────────
# pdfs_to_upload/ is auto-filled during enrich and never auto-cleaned, so its
# file count drifts from the DB. Everything here is driven by the *current*
# PDF-only papers (unconfirmed pdf_saved, with a PDF on disk) so counts match
# the "PDF only" status metric.

def pdf_only_papers(db: Database) -> list[dict]:
    """Papers still needing manual PDF import: pdf_saved, not yet confirmed, with
    their PDF present on disk. On the cloud the filesystem is ephemeral, so a
    paper counted under "PDF only" can have a path whose file no longer exists —
    those are excluded here (only downloadable files are returned)."""
    out = []
    for p in db.all_papers():
        if p.get("metadata_source") == "pdf_saved" and not p.get("pdf_confirmed"):
            lp = p.get("local_pdf_path")
            if lp and Path(lp).exists():
                out.append(p)
    return out


def zip_pdf_only(db: Database) -> tuple[bytes, int]:
    """Build an in-memory zip of every downloadable PDF-only paper's file, for the
    lab member to download and drag into Zotero desktop. Returns (zip_bytes,
    count). Empty zip (count 0) when none of the files are present this session —
    the expected case after a cloud restart wipes the ephemeral filesystem."""
    import io
    import zipfile
    buf = io.BytesIO()
    n = 0
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in pdf_only_papers(db):
            src = Path(p["local_pdf_path"])
            arc, i = src.name, 1
            while arc in used:        # avoid clobbering same-named PDFs in the zip
                arc = f"{src.stem}_{i}{src.suffix}"
                i += 1
            used.add(arc)
            zf.write(src, arc)
            n += 1
    return buf.getvalue(), n


# ── Ingest orchestration ────────────────────────────────────────────────────
# Mirrors cli.cmd_ingest: hash-dedup against processed_files, parse, add papers
# + comments, mark files processed. Returns a summary dict for the UI.

def ingest_paths(db: Database, paths: list[str], force: bool = False,
                 progress=None) -> dict:
    """Ingest Slack export files. By default a file whose hash is already in the
    processed-files registry is skipped (so curated-away papers aren't resurrected
    on a re-drop). `force=True` ignores that registry and re-parses every file —
    the recovery path when the DB and registry have desynced (e.g. the DB was
    wiped, or a git merge paired an empty DB with a full registry). Re-parsing is
    safe: papers dedup by URL and comments by ts, so nothing duplicates."""
    pending: list[tuple[str, str, str]] = []   # (path, basename, hash)
    skipped = 0
    for path in paths:
        name = os.path.basename(path)
        try:
            h = hash_file(path)
        except OSError:
            continue
        if not force and db.is_file_processed(name, h):
            skipped += 1
            continue
        pending.append((path, name, h))

    if not pending:
        return {"new_papers": 0, "new_comments": 0, "files": 0, "skipped": skipped}

    parsed_by_path = {pf["path"]: pf for pf in parse_files([p for p, _, _ in pending])}
    new_papers = new_comments = files_done = slack_files = 0

    for path, name, h in pending:
        pf = parsed_by_path.get(path)
        if pf is None:
            continue
        file_urls: set[str] = set()
        for link in pf["links"]:
            file_urls.add(link["url"])
            key, is_new = db.add_paper(link["url"], doi=link["doi"])
            for c in link["comments"]:
                if db.add_comment(key, c):
                    new_comments += 1
            if is_new:
                new_papers += 1
        # Slack file uploads (files[]): add by permalink and RECORD the download
        # link + filename. The bytes are fetched + LLM-read later by the normal
        # Enrich run (metadata.enrich_paper's Slack-file branch) — no separate step.
        for fa in pf.get("files", []):
            file_urls.add(fa["url"])
            key, is_new = db.add_paper(fa["url"])
            for c in fa["comments"]:
                if db.add_comment(key, c):
                    new_comments += 1
            if is_new:
                new_papers += 1
            p = db.get_paper(fa["url"])
            if p is None or p.get("local_pdf_path") or p.get("slack_file_url"):
                continue               # already downloaded or already recorded
            db.update_paper(normalize_url(fa["url"]),
                            slack_file_url=fa["download_url"],
                            slack_file_name=fa["name"])
            slack_files += 1
        db.mark_file_processed(name, h, len(file_urls), pf["messages"])
        files_done += 1

    db.save(progress=progress)   # the network-bound step on Turso; drives the bar
    return {"new_papers": new_papers, "new_comments": new_comments,
            "files": files_done, "skipped": skipped, "slack_files": slack_files}
