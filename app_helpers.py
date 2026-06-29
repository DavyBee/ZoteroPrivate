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
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterator, Optional

import config
from database import Database, hash_file, normalize_url
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

# translator/crossref are auto-approved. LLM records must be human-reviewed
# before they count as Ready (see llm_review_queue / zotero_client.uploadable_papers).
READY_SOURCES = ("translator", "crossref_doi")


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
    db.delete_paper removes the PDF and saves on each call. Returns the count."""
    n = 0
    for url in urls:
        if db.delete_paper(url):     # delete_paper removes PDFs + saves internally
            n += 1
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
    comments). Tweets that resolve (link found or confirmed link-free) are
    flagged `tweet_expanded` so they aren't fetched again; tweets that ERROR
    (e.g. X's endpoint is down) are left unflagged so they retry next time.
    progress_cb(done, total) if given. Returns {scanned, links_found,
    new_papers, failed}."""
    work = [p for p in db.all_papers()
            if is_tweet_url(p.get("url", "")) and not p.get("tweet_expanded")]
    total = len(work)
    scanned = links = new = failed = 0
    for p in work:
        url = p["url"]
        scanned += 1
        status, target = expand_tweet(url)
        if status == "error":
            failed += 1  # leave unflagged → retried when the endpoint is reachable
        else:
            db.update_paper(normalize_url(url), tweet_expanded=True, tweet_target=target)
            if status == "ok":
                links += 1
                key, is_new = db.add_paper(target)
                for c in p.get("comments", []) or []:
                    db.add_comment(key, c)
                if is_new:
                    new += 1
                # The paper the tweet pointed to now has its own record; the tweet
                # itself is just a pointer, so blank its (paper-derived) metadata
                # and file it in the Links tab as a plain bookmark. With no
                # title/DOI it can't collide with the extracted paper in Duplicates.
                make_plain_link(db, url)
        if progress_cb:
            progress_cb(scanned, total)
    db.save()
    return {"scanned": scanned, "links_found": links,
            "new_papers": new, "failed": failed}


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
    "ready":   {"translator", "crossref_doi"},
    "pdf":     {"pdf_saved"},
    "llm":     {"llm"},
    "triage":  {"needs_manual"},
    "links":   {"link"},
    "skipped": {"non_paper"},
}


def debug_reset(db: Database, action: str) -> str:
    """Run one debug reset and save. Returns a human-readable result line."""
    # Baseline restore writes the DB file directly and must NOT be followed by
    # db.save() (which would clobber the restore with the stale in-memory DB) —
    # the caller reloads from disk afterwards.
    if action == "revert_review":
        if revert_to_baseline():
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


# ── Native macOS file/folder pickers (osascript) ──────────────────────────────

def finder_pick_files() -> list[str]:
    """Native multi-file picker for Slack JSON exports. [] on cancel/error."""
    script = (
        'set fs to choose file with prompt "Select Slack export JSON files" '
        'of type {"json"} with multiple selections allowed\n'
        'set out to ""\n'
        'repeat with f in fs\n  set out to out & POSIX path of f & "\n"\nend repeat\n'
        'return out'
    )
    return [line for line in _osascript(script).splitlines() if line.strip()]


def finder_pick_folder() -> Optional[str]:
    """Native folder picker for the PDF export. None on cancel/error."""
    script = ('set d to choose folder with prompt '
              '"Choose a folder to save the PDFs into"\nreturn POSIX path of d')
    return _osascript(script).strip() or None


def open_urls(urls: list[str]) -> int:
    """Open each URL as a new tab in the running default browser (macOS `open`),
    de-duped, blanks dropped. We deliberately use the *default* browser (not a
    hardcoded one): that's where the lab member's Zotero connector is installed.
    Used by the Access-issues / Links / Likely-junk tabs so the connector can be
    run on every page in one go — or so they can eyeball which are simply down (a
    Cloudflare challenge, a paywall, and a dead 404 look different to a human even
    though the fetcher can't reliably tell them apart). Returns count opened.

    We do NOT pass `open -n`: that launches a NEW browser instance, which Chrome/
    Safari answer by restoring the current session's tabs into the new window and
    then appending ours — the lab saw their existing tabs get duplicated. Plain
    `open` reuses the running browser and adds the links as tabs, which is the
    expected behaviour. (A genuine fresh window isn't reachably reliable across
    arbitrary default browsers without hardcoding one, so tabs it is.)"""
    seen: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.append(u)
    if not seen:
        return 0
    subprocess.run(["open", *seen])
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


def _osascript(script: str) -> str:
    try:
        res = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=300)
        return res.stdout
    except Exception:
        return ""


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
    if src in ("translator", "crossref_doi", "llm") and title:
        return "llm", f"✓ {src}{pdf}: {title[:60]}"
    if src in ("translator", "crossref_doi", "llm"):
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

    Re-entrancy-safe: the index only advances AFTER a paper is enriched, so an
    abandoned rerun re-does (never skips) that paper; and an already-enriched
    paper (an overlapping rerun beat us to it) is skipped without re-counting."""
    urls = state["urls"]
    total = len(urls)
    i = state["idx"]

    if i >= total:
        db.save()
        snapshot_baseline()   # checkpoint post-enrich state for "Undo Review changes"
        return {"done": True, "i": state["done"], "total": total,
                "counts": dict(state["counts"])}

    url = urls[i]
    paper = db.get_paper(url)
    if paper is None or paper.get("metadata_source", "none") != "none":
        state["idx"] = i + 1   # deleted, or already enriched by an overlapping run
        return {"skip": True, "i": state["done"], "total": total, "url": url,
                "counts": dict(state["counts"]), "done": False, "halted": False}

    fields = enrich_paper(paper, anthropic_key, audit_path)
    db.update_paper(normalize_url(url), **fields)
    category, label = _outcome(fields)
    state["counts"][category] += 1
    if fields.get("local_pdf_path"):
        state["counts"]["pdf_total"] += 1
    state["done"] += 1
    state["idx"] = i + 1       # advance only after a successful enrich
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

def _baseline_path() -> Path:
    return config.STATE_DIR / "baseline.db"


def has_baseline() -> bool:
    return _baseline_path().exists()


def snapshot_baseline() -> None:
    """Save the current DB as the post-enrichment baseline."""
    src = Path(config.DB_PATH)
    if src.exists():
        shutil.copy(src, _baseline_path())


def ensure_baseline() -> None:
    """Create a baseline from the current DB if none exists yet, so an already-
    enriched DB loaded at startup still has a checkpoint to revert to."""
    if not has_baseline():
        snapshot_baseline()


def revert_to_baseline() -> bool:
    """Restore the DB from the post-enrichment baseline, then clear every review
    acceptance. Returns False if no baseline exists.

    Clearing `reviewed` is essential: the baseline is snapshotted at enrich time,
    so a review-then-enrich sequence captures the acceptances *into* the baseline.
    Without this, restoring it wouldn't put accepted LLM items back in the review
    queue (they'd already be marked reviewed). Post-enrichment, nothing is
    reviewed, so dropping all acceptances is the correct "undo review" state.
    """
    bp = _baseline_path()
    if not bp.exists():
        return False
    shutil.copy(bp, Path(config.DB_PATH))
    db = Database.load(str(config.DB_PATH), str(config.PROCESSED_FILES_PATH))
    if any(p.get("reviewed") for p in db.all_papers()):
        for p in db.all_papers():
            p["reviewed"] = None
        db.save()
    return True


# ── Single-step undo (per-action, distinct from the post-enrich baseline) ─────
# Each undoable action copies the DB aside *before* it runs; "Undo" restores it.
# One level deep (the last action). Note: undoing a DELETE brings the records
# back, but a deleted paper's PDF was already removed from disk — re-enrich
# re-fetches it.

def _undo_path() -> Path:
    return config.STATE_DIR / "undo.db"


def snapshot_undo() -> None:
    """Copy the current DB aside as the pre-action state for a one-step Undo."""
    src = Path(config.DB_PATH)
    if src.exists():
        shutil.copy(src, _undo_path())


def restore_undo() -> bool:
    """Restore the DB from the last pre-action snapshot. False if none exists."""
    up = _undo_path()
    if not up.exists():
        return False
    shutil.copy(up, Path(config.DB_PATH))
    return True


# ── PDF export (driven by the DB, not the stale pdfs_to_upload/ cache) ────────
# pdfs_to_upload/ is auto-filled during enrich and never auto-cleaned, so its
# file count drifts from the DB. Everything here is driven by the *current*
# PDF-only papers (unconfirmed pdf_saved, with a PDF on disk) so counts match
# the "PDF only" status metric.

def upload_queue_dir() -> Path:
    return config.PROJECT_ROOT / "pdfs_to_upload"


def clear_upload_queue() -> int:
    """Empty the pdfs_to_upload/ drag-in folder (PDF files only; the folder
    stays). Called after the lab member confirms they've dragged the folder into
    Zotero desktop, so the staging area doesn't keep showing already-imported
    PDFs. The canonical pdfs/ store is left untouched — enrich re-stages a copy
    here if a new pdf_saved paper appears. Returns the count removed."""
    queue = upload_queue_dir()
    if not queue.exists():
        return 0
    n = 0
    for f in queue.iterdir():
        if f.suffix.lower() == ".pdf":
            f.unlink()
            n += 1
    return n


def pdf_only_papers(db: Database) -> list[dict]:
    """Papers still needing manual PDF import: pdf_saved, not yet confirmed, with
    their PDF present on disk."""
    out = []
    for p in db.all_papers():
        if p.get("metadata_source") == "pdf_saved" and not p.get("pdf_confirmed"):
            lp = p.get("local_pdf_path")
            if lp and Path(lp).exists():
                out.append(p)
    return out


def export_pdfs(db: Database, dest: str | Path, wipe: bool = False) -> int:
    """Copy the current PDF-only papers' PDFs into `dest`. If wipe, clear any
    existing PDFs in `dest` first (used to rebuild the default queue folder so
    it can't go stale). Returns the count copied."""
    dest = Path(dest)
    if wipe and dest.exists():
        for f in dest.iterdir():
            if f.suffix.lower() == ".pdf":
                f.unlink()
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in pdf_only_papers(db):
        src = Path(p["local_pdf_path"])
        shutil.copy2(src, dest / src.name)
        n += 1
    return n


# ── Ingest orchestration ────────────────────────────────────────────────────
# Mirrors cli.cmd_ingest: hash-dedup against processed_files, parse, add papers
# + comments, mark files processed. Returns a summary dict for the UI.

def ingest_paths(db: Database, paths: list[str], force: bool = False) -> dict:
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

    db.save()
    return {"new_papers": new_papers, "new_comments": new_comments,
            "files": files_done, "skipped": skipped, "slack_files": slack_files}
