"""
app.py — Streamlit GUI for the Slack→Zotero pipeline.

The primary interface for lab members. Replaces all CLI interaction for
non-engineers; the CLI stays for debug/power use. This layer holds NO pipeline
logic — it calls existing backend functions (via app_helpers where loops or
table-building are involved) and renders the result.

Run with:   streamlit run app.py

Four tabs (Ingest · Enrich · Review · Upload) sit under a persistent status bar
that shows the seven user-facing states computed fresh from the live DB.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime

import streamlit as st

import app_helpers as H
import config
import zotero_client
from database import DatabaseError, normalize_url

st.set_page_config(page_title="Slack to Zotero", page_icon="", layout="wide")


# ── Shared state ────────────────────────────────────────────────────────────

def get_db():
    """The Database lives in session_state; disk is read only on first run
    and after ingest (which calls reload_db)."""
    if "db" not in st.session_state:
        try:
            st.session_state.db = H.load_db()
        except DatabaseError as exc:
            # A hand-edited / corrupted state file. Show a clear, actionable
            # message and halt rather than crashing with a raw traceback. The
            # file is left untouched, so nothing is lost.
            st.error("The database couldn't be read.")
            st.code(str(exc))
            st.info("Reload the page. If this keeps happening, the data may be "
                    "corrupted.")
            st.stop()
        H.ensure_baseline(st.session_state.db)   # so an already-enriched DB has a checkpoint to revert to
    return st.session_state.db


def _is_admin() -> bool:
    """Debug mode is restricted to the owner on the hosted deploy.
    Local dev (no TURSO) → always allow. Cloud → require ADMIN_TOKEN
    entered via the Settings tab (stored in session state for the session)."""
    if not os.environ.get("TURSO_DATABASE_URL"):
        return True   # local dev
    return st.session_state.get("_admin_unlocked", False)


def refresh_tables():
    """Give every dataframe a fresh widget key on the next render — WITHOUT
    re-reading the library from disk.

    Streamlit keys row-selection state to the widget key; without bumping it, a
    stale selection (old row indices) survives a delete/reclassify and points at
    the wrong rows — or past the end, crashing with IndexError.

    Use this after an in-memory mutation: the review/upload helpers mutate the
    session db in place AND call db.save(), so the object is already authoritative
    and persisted. Re-reading from disk would only duplicate that — and on the
    hosted Turso DB a reload is a network fetch of every paper, which is what made
    Review actions slow. reload_db() is for when disk changed *outside* the
    in-memory object (first load, undo/baseline restore)."""
    st.session_state.table_gen = st.session_state.get("table_gen", 0) + 1


def reload_db():
    """Re-read the canonical DB from disk, then refresh tables. Only needed when
    the on-disk state diverged from the in-memory object (initial load, or an
    undo/baseline restore that overwrote the file). After ordinary mutations use
    refresh_tables() — a full reload over Turso per action is the slow path."""
    st.session_state.db = H.load_db()
    refresh_tables()


def _undoable(label: str):
    """Snapshot the in-memory DB state before an action so it can be undone, and
    stash the label for the Undo banner. Call right BEFORE the mutation. The
    snapshot is an in-RAM copy kept in session_state — cheap (no DB write), and a
    one-step, this-session undo by design."""
    st.session_state.undo_state = get_db().snapshot_state()
    st.session_state.undo_label = label


def render_undo_banner():
    """One-step Undo for the last action (Move to Ready, Delete, etc.). Shows
    until the next action replaces it or it's used.

    The banner always reserves its slot (an empty container when there's nothing
    to undo) so the element tree above the main tabs is identical whether or not
    an undo is pending. Otherwise the first action — which is what first populates
    the banner — would insert elements above st.tabs and shift its position,
    making Streamlit reset the active tab back to Ingest (only on that first
    action, since the banner is already present for every later one)."""
    slot = st.container()
    label = st.session_state.get("undo_label")
    if not label:
        return
    with slot:
        c1, c2 = st.columns([5, 1])
        c1.info(f"✓ {label}")
        if c2.button("↩ Undo", key="undo_last"):
            snap = st.session_state.pop("undo_state", None)
            if snap is not None:
                get_db().restore_state(snap)   # restores in memory + incremental save
                st.session_state.pop("undo_label", None)
                refresh_tables()
                st.toast("Reverted.")
            st.rerun()


# ── Status bar + environment banner ─────────────────────────────────────────

def render_status_bar(db):
    all_p = db.all_papers()
    c = H.status_counts(db)
    # Every status-bar number is the length of the exact paper list its tab
    # renders, so the bar and the tabs can never disagree. Ready and LLM come
    # from the tab functions; Pending / Uploaded / Upload error / PDF only have
    # no tab of their own and come from status_counts.
    n_ready = len(zotero_client.uploadable_papers(db))
    n_llm = len(H.llm_review_queue(db))
    n_triage = len(H.triage_papers(db))
    n_link = len(H.link_papers(db))
    n_junk = len(H.junk_papers(db))
    n_err = c["Upload error"]
    # Everything that's been enriched but not yet uploaded — derived as the
    # remainder so it stays correct however the sub-buckets are sliced.
    enriched = len(all_p) - c["Pending"] - c["Uploaded"]
    # Three stages: pending → enriched (not yet uploaded) → uploaded. The
    # Enriched breakdown lives *inside* its (widened) column so it stays anchored
    # under that number at any width. Green = ready; yellow = needs action.
    a, b, d = st.columns([1, 2, 1])
    a.metric("Pending enrichment", c["Pending"])
    b.metric("Enriched (not uploaded)", enriched)
    d.metric("Uploaded", c["Uploaded"])
    lines = [
        f":green[**{n_ready}** Ready]",
        f":orange[**{n_llm}** LLM] · :orange[**{c['PDF only']}** PDF only]",
        f":orange[**{n_triage}** Access issues] · :orange[**{n_link}** Links]",
        f":orange[**{n_junk}** Likely junk]",
    ]
    if n_err:
        lines.append(f":red[**{n_err}** Upload errors — fix in the Upload tab]")
    b.markdown("  \n".join(lines))


def render_env_banner():
    if not config.anthropic_api_key():
        st.info("No anthropic key set, so claude cannot be used.")


# ── Tab 1: Ingest ───────────────────────────────────────────────────────────

def tab_ingest(db):
    st.subheader("Ingest Slack exports")
    flash = st.session_state.pop("ingest_result", None)
    if flash:
        st.success(flash)
    warn = st.session_state.pop("ingest_warn", None)
    if warn:
        st.warning(warn)
    st.caption("Upload slack jsons here. Previously uploaded files will be skipped.")

    # The uploader's key carries a generation counter; bumping it after an ingest
    # gives a fresh empty uploader, so the files (and the Ingest button) clear
    # instead of lingering and being re-offered every rerun.
    gen = st.session_state.get("uploader_gen", 0)
    uploaded = st.file_uploader("Slack export JSON files", accept_multiple_files=True,
                                type="json", key=f"uploader_{gen}")
    c1, c2 = st.columns(2)
    go = c1.button(f"📥 Ingest {len(uploaded)} uploaded file(s)" if uploaded
                   else "📥 Ingest uploaded files", type="primary", disabled=not uploaded)
    pick = c2.button("📁 Pick from Finder & ingest")

    paths: list[str] = []
    if go and uploaded:
        tmpdir = tempfile.mkdtemp(prefix="slack_ingest_")
        for f in uploaded:
            p = os.path.join(tmpdir, f.name)
            with open(p, "wb") as out:
                out.write(f.getbuffer())
            paths.append(p)
    if pick:
        paths.extend(H.finder_pick_files())

    if paths:
        bar = st.progress(0.0, text=f"Ingesting {len(paths)} file(s) — parsing…")
        # Parsing/adding is in-memory and fast; the bar moves during the save,
        # which on the hosted Turso DB is the only network-bound (slow) step.
        result = H.ingest_paths(
            db, paths,
            progress=lambda f: bar.progress(f, text="Saving to the database…"))
        bar.empty()
        msg = (f"Added {result['new_papers']} sources and "
               f"{result['new_comments']} comments from {result['files']} slack jsons. "
               f"{result['skipped']} files were previously processed from the "
               "uploaded jsons.")
        if result.get("slack_files"):
            msg += (f" Found {result['slack_files']} uploaded files. They'll be read "
                    "during enrich.")
        # Desync guard: everything skipped and nothing added. If the DB is also
        # empty, the registry is almost certainly stale — point at the Debug fix.
        if result["skipped"] and not result["new_papers"] and not db.all_papers():
            st.session_state.ingest_warn = (
                "The website record shows all jsons were already ingested and "
                "processed, but the database also seems empty. Try force re-ingesting "
                "the files in the debug page (at the bottom) to fix this.")
        st.session_state.ingest_result = msg
        st.session_state.uploader_gen = gen + 1   # reset uploader → clears the button
        refresh_tables()   # ingest_paths already mutated the session db + saved
        st.rerun()


# ── Tab 2: Enrich ───────────────────────────────────────────────────────────

def _model_status(force: bool = False) -> str:
    """Cached Anthropic model-availability check (one API call per model per
    session). Re-checks when the configured model changes or force=True."""
    model, key = config.llm_model(), config.anthropic_api_key()
    cache = st.session_state.get("model_status")
    if force or not cache or cache.get("model") != model:
        cache = {"model": model, "status": H.llm_model_status(key, model)}
        st.session_state.model_status = cache
    return cache["status"]


def _credit_status(force: bool = False) -> str:
    """Cached Anthropic credit check (one 1-token call per session). Re-checks
    when the configured model changes or force=True."""
    model, key = config.llm_model(), config.anthropic_api_key()
    cache = st.session_state.get("credit_status")
    if force or not cache or cache.get("model") != model:
        cache = {"model": model, "status": H.llm_credit_status(key, model)}
        st.session_state.credit_status = cache
    return cache["status"]


def _model_deprecated_banner():
    """Red alert when the configured Claude model has been retired. Shown only
    when a key is set (no point if the LLM stage is skipped anyway)."""
    if config.anthropic_api_key() and _model_status() == "unavailable":
        st.error(f"Model `{config.llm_model()}` is no longer available. The LLM step "
                 "will fail until you pick a current model in Settings.")


def tab_enrich(db):
    st.subheader("Enrich pending papers")
    _model_deprecated_banner()

    tw = st.session_state.pop("tweet_result", None)
    if tw:
        st.success(tw[0])
        if tw[1]:
            st.warning(tw[1])
    er = st.session_state.pop("enrich_result", None)
    if er:
        st.success(er)

    _enrich_errors_panel(db)

    # ── Tweet expansion ──
    n_tw = H.unexpanded_tweet_count(db)
    tw_busy = st.session_state.get("expanding", False)
    if n_tw or tw_busy:
        st.caption(f"{n_tw} tweets contain links. Click this to add these links as "
                   "separate sources.")
        if st.button(f"🐦 Expand {n_tw} tweet link(s)", disabled=tw_busy or not n_tw):
            st.session_state.expanding = True
            st.rerun()
        if tw_busy:
            _run_expand_ui(db)
        st.divider()

    # ── Enrich ──
    pending = db.unenriched_papers()
    enr_busy = st.session_state.get("enriching", False)
    st.caption(f"{len(pending)} waiting to enrich")
    if not pending and not enr_busy:
        st.info("Nothing to enrich.")
        return
    if not enr_busy:
        if st.button("▶ Enrich all pending", type="primary", disabled=not pending):
            st.session_state.enriching = True
            st.session_state.enrich_paused = False
            st.session_state.enrich_lines = []
            st.session_state.enrich_last = None
            # Plain resumable state (URL list + index), NOT a live generator —
            # see app_helpers.enrich_init for why (Streamlit rerun re-entrancy).
            st.session_state.enrich_state = H.enrich_init(db)
            st.rerun()
    else:
        _run_enrich_step(db)


def _enrich_errors_panel(db):
    """Surface papers that errored on their last enrichment attempt AND are still
    stuck — an API/parse failure or a soft-dependency miss (Citoid down, page
    blocked) recorded in `last_error`. A burst of identical errors usually means
    a transient outage (Citoid, Anthropic); just Enrich again once it clears.

    Only papers still in a Pending or Upload-error state are shown: a stored
    `last_error` lingers on a record even after you've triaged it (moved it to
    Ready, marked it added to Zotero, etc.), so we gate on the *current* state to
    avoid nagging about errors you've already resolved. Items sorted into named
    queues (Access issues, Links, …) are handled from those tabs instead."""
    errored = [p for p in db.all_papers()
               if p.get("last_error") and H.ui_state(p) in ("Pending", "Upload error")]
    if not errored:
        return
    with st.expander(f"{len(errored)} errored on the last enrich", expanded=False):
        st.caption("There was an error during enrich (maybe the server's "
                   "connections). Try again.")
        for p in errored[:50]:
            url = p.get("url", "")
            short = url if len(url) <= 80 else url[:77] + "…"
            st.markdown(f"- [{short}]({url}) — `{p.get('last_error')}`")
        if len(errored) > 50:
            st.caption(f"…and {len(errored) - 50} more.")


def _run_expand_ui(db):
    bar = st.progress(0.0, text="Looking up tweet links…")
    r = H.expand_tweets(db, progress_cb=lambda d, t: bar.progress(
        d / max(t, 1), text=f"Checked {d}/{t} tweet(s)…"))
    bar.empty()
    st.session_state.expanding = False
    warn = (f"{r['failed']} tweets couldn't be read (X may be down). Kept as plain "
            "links.") if r["failed"] else None
    # Explain how the counts relate, so "found 19 links, added 18 papers" doesn't
    # look like a glitch: a tweet may share no link, or share one we already have.
    scanned, found, added = r["scanned"], r["links_found"], r["new_papers"]
    dupes = found - added                       # links that were already in the library
    no_link = scanned - r["failed"] - found     # tweets with nothing to add
    bits = [f"Checked {scanned} tweet(s)."]
    if found:
        if added and dupes:
            were = "was" if dupes == 1 else "were"
            bits.append(f"{found} pointed to a paper or link — added {added} as new "
                        f"item(s) to enrich. The other {dupes} {were} duplicate "
                        "link(s): the same paper was shared by more than one tweet "
                        "(or was already saved), so it's added only once.")
        elif added:
            bits.append(f"Added all {added} linked paper(s)/item(s) to enrich.")
        else:
            bits.append(f"All {found} linked item(s) were already in your library — "
                        "nothing new to add.")
    else:
        bits.append("None of them shared a paper or link to add.")
    if no_link:
        bits.append(f"({no_link} contained no shareable link.)")
    st.session_state.tweet_result = (" ".join(bits), warn)
    refresh_tables()   # expand_tweets mutated the session db in place + saved
    st.rerun()


def _run_enrich_step(db):
    """Advance the enrich run one paper per rerun so Pause/Stop stay responsive.
    (A single in-script loop would block every button click until it finished.)
    State is plain data in session_state (URL list + index) — NOT a live
    generator, so an overlapping rerun from any button can't re-enter it
    ('generator already executing') or wedge the run. See app_helpers.enrich_init."""
    state = st.session_state.get("enrich_state")
    if state is None:                       # lost state (e.g. hot reload) → end
        _finish_enrich(stopped=True)
        st.rerun()
        return
    paused = st.session_state.get("enrich_paused", False)
    lines = st.session_state.get("enrich_lines", [])
    last = st.session_state.get("enrich_last")

    # Controls.
    c1, c2 = st.columns(2)
    if paused:
        if c1.button("▶ Resume", key="enrich_resume", type="primary"):
            st.session_state.enrich_paused = False
            st.rerun()
    else:
        if c1.button("⏸ Pause", key="enrich_pause"):
            db.save()                       # persist progress at the pause point
            st.session_state.enrich_paused = True
            st.rerun()
    if c2.button("⏹ Stop", key="enrich_stop"):
        db.save()
        _finish_enrich(stopped=True)
        st.rerun()

    # Progress + scrollable log, rendered from session_state every rerun.
    if last and last.get("total"):
        total = last["total"]
        # Clamp: an overlapping rerun can re-do one paper and nudge the count a
        # hair past total, and st.progress rejects anything outside [0, 1].
        done = min(last.get("i", 0), total)
        st.progress(done / total, text=f"Enriching {done}/{total}…")
    if lines:
        st.container(height=300).code("\n".join(reversed(lines)))

    if paused:
        st.info("Paused. Resume or stop. Progress is saved either way.")
        return

    # Advance one paper, then rerun to do the next.
    step = H.enrich_step(db, state, config.anthropic_api_key(),
                         str(config.LLM_AUDIT_PATH))
    if step.get("done"):
        st.session_state.enrich_last = step      # carries final counts for the summary
        _finish_enrich(stopped=False)
        st.rerun()
        return
    if not step.get("skip"):                     # a real paper was processed
        short = step["url"] if len(step["url"]) <= 70 else step["url"][:67] + "…"
        lines.append(f"[{step['i']}/{step['total']}] {step['label']}  —  {short}")
        st.session_state.enrich_lines = lines
        st.session_state.enrich_last = step
    st.rerun()


def _finish_enrich(stopped=False):
    """Tear down enrich session_state and stash a summary for the next render."""
    last = st.session_state.get("enrich_last")
    if last:
        c = last["counts"]
        prefix = "Stopped — partial run: " if stopped else "Done: "
        st.session_state.enrich_result = (
            prefix + f"{c['ok']} ready, {c['llm']} llm, {c['manual']} access issues, "
            f"{c['link']} other links, {c['non']} junk, {c['fail']} failed "
            f"({c['pdf']} PDF only saved). "
            f"With these, we also saved {c['pdf_total']} total PDFs.")
    st.session_state.enriching = False
    st.session_state.enrich_paused = False
    for k in ("enrich_state", "enrich_lines", "enrich_last"):
        st.session_state.pop(k, None)
    refresh_tables()   # enrich mutated the session db in place + saved


# ── Tab 3: Review ───────────────────────────────────────────────────────────

# Give the URL column a generous starting width so long links are mostly visible
# in their own cell, and let it be dragged wider for the rest.
#
# Streamlit always stretches the *last* column to fill the grid, which pins that
# column's width and removes its resize handle — so when URL is last it can't be
# dragged. We append a blank "spacer" column after URL: the spacer becomes the
# stretched/last column, and URL keeps a normal draggable right edge. Drag it
# wider to read a long link in full (or scroll right when the grid overflows).
# The spacer holds no data and is non-editable, so it never affects edits/saves.
_LINK_COL = st.column_config.LinkColumn("URL", display_text=None, width=600)

_SPACER = " "  # blank header; trailing filler so URL isn't the pinned last column
_SPACER_COL = st.column_config.Column(_SPACER, disabled=True)

# Use this for every table's column_config; pair it with _spaced(rows) below.
_COLS = {"URL": _LINK_COL, _SPACER: _SPACER_COL}


def _spaced(rows: list[dict]) -> list[dict]:
    """Append the blank spacer column to each row so URL gets a draggable edge."""
    return [{**r, _SPACER: ""} for r in rows]


def _tkey(key: str) -> str:
    """Append the table generation so the widget (and its selection) is reset
    whenever the data changes — see reload_db()."""
    return f"{key}_{st.session_state.get('table_gen', 0)}"


_REVIEW_TABLE_H = 600


def _table(rows: list[dict], key: str):
    """Render a multi-select dataframe with a clickable URL column.
    Returns the list of selected row indices (clamped to valid rows)."""
    event = st.dataframe(
        _spaced(rows), key=_tkey(key), hide_index=True, width="stretch",
        height=_REVIEW_TABLE_H, on_select="rerun", selection_mode="multi-row",
        column_config=_COLS,
    )
    return [i for i in event.selection.rows if i < len(rows)]


def _review_table(db, papers, key, rows=None):
    """Metadata table for a Review sub-tab. With Edit mode on it's an editable
    data_editor over the standard Title/Authors/Year/Journal columns that
    auto-saves every edit, and returns None — editing works on every review tab.
    With Edit mode off it's a multi-select dataframe over `rows` if given (a
    tab-specific read-only view), else paper_rows; returns the selected indices."""
    if st.session_state.get("edit_mode"):
        # No Save button: a committed cell edit triggers a rerun, the editor
        # returns the new value, and apply_table_edits persists it (a no-op when
        # nothing changed). Editing metadata never moves a paper between tabs, so
        # there's nothing else to refresh — no reload/rerun needed.
        edited = st.data_editor(
            _spaced(H.paper_rows(papers)), key=_tkey(key), hide_index=True,
            width="stretch", height=_REVIEW_TABLE_H, disabled=["URL", "DOI"],
            column_config=_COLS,
        )
        H.apply_table_edits(db, papers, edited)
        return None
    return _table(rows if rows is not None else H.paper_rows(papers), key)


def _curate_tab(db, papers, promote, key, rows=None):
    """One standardized non-Ready review sub-tab: the metadata table (editable in
    Edit mode) above a fixed action row that's ALWAYS visible — Move all to Ready
    · Move selected to Ready · Delete selected. Every non-Ready tab uses this so
    they all behave the same. `promote(db, urls)` is the tab's move-to-Ready
    action (accept for LLM, link-resolve for the rest); `rows` is the read-only
    view shown when not editing (triage badge, expanded-tweet target, …).

    Edit mode has no row selection, so the two 'selected' buttons disable; cell
    edits auto-save as you make them. Buttons disable (rather than vanish) when
    they don't apply, so the action row is always present and in the same place.

    Returns the selected row indices (empty in Edit mode) so a caller can add a
    tab-specific action below — e.g. Access issues' 'Open selected links'."""
    n = len(papers)
    if st.session_state.get("edit_mode"):
        # Auto-save: a committed cell edit reruns, the editor returns the new
        # value, and apply_table_edits persists it (see _review_table). The
        # move/delete actions stay in the row below.
        edited = st.data_editor(
            _spaced(H.paper_rows(papers)), key=_tkey(key), hide_index=True,
            width="stretch", height=_REVIEW_TABLE_H, disabled=["URL", "DOI"],
            column_config=_COLS,
        )
        H.apply_table_edits(db, papers, edited)
        sel = []
    else:
        sel = _table(rows if rows is not None else H.paper_rows(papers), key)

    c1, c2, c3 = st.columns(3)
    if c1.button(f"✅ Move all {n} to Ready", key=key + "_all", disabled=not n,
                 help="Promote every item in this tab to Ready."):
        _undoable(f"Moved all {n} to Ready")
        with st.spinner("Moving to Ready…"):
            promote(db, [p["url"] for p in papers])
        refresh_tables(); st.rerun()
    if c2.button("☑ Move selected to Ready", key=key + "_sel", disabled=not sel):
        _undoable(f"Moved {len(sel)} to Ready")
        with st.spinner("Moving to Ready…"):
            promote(db, [papers[i]["url"] for i in sel])
        refresh_tables(); st.rerun()
    if c3.button("🗑 Delete selected", key=key + "_del", disabled=not sel):
        _undoable(f"Deleted {len(sel)} paper(s)")
        _delete_selected(db, papers, sel); st.rerun()
    return sel


def _delete_selected(db, papers, selected_idx):
    urls = [papers[i]["url"] for i in selected_idx if i < len(papers)]
    H.delete_papers(db, urls)               # also removes each paper's PDFs
    refresh_tables()                        # delete_papers mutated the session db + saved


def _open_links_row(papers, sel, key):
    """Open-in-browser actions for the Links / Likely-junk tabs: open the selected
    rows, or every row in the tab — each in a NEW browser window so the lab member
    can eyeball them (and run the Zotero connector if something turns out worth
    keeping). No reload, so the dataframe selection survives the click."""
    o1, o2 = st.columns(2)
    if o1.button(f"🌐 Open {len(sel)} selected in browser", key=key + "_open_sel",
                 disabled=not sel,
                 help="Open the selected links in a new browser window."):
        n = H.open_urls([papers[i]["url"] for i in sel])
        st.toast(f"Opened {n} link(s) in your browser.")
    if o2.button(f"🌐 Open all {len(papers)} in browser", key=key + "_open_all",
                 disabled=not papers,
                 help="Open every link in this tab in a new browser window."):
        n = H.open_urls([p["url"] for p in papers])
        st.toast(f"Opened {n} link(s) in your browser.")


def tab_review(db):
    st.subheader("Review & curate")
    st.toggle("✏️ Edit table cells", key="edit_mode",
              help="Edit Title / Authors / Year / Journal inline — changes save "
                   "automatically. Row actions are hidden while editing.")
    sub = st.tabs(["Ready", "LLM", "Access issues", "Links", "Likely junk",
                   "Duplicates"])

    # — Ready (auto-approved + accepted LLM) —
    with sub[0]:
        papers = zotero_client.uploadable_papers(db)
        st.caption(f"{len(papers)} ready to upload to Zotero")
        sel = _review_table(db, papers, "tbl_ready")
        if sel and st.button("🗑 Delete selected", key="del_ready"):
            _undoable(f"Deleted {len(sel)} paper(s)")
            _delete_selected(db, papers, sel)
            st.rerun()

    # — LLM (needs human acceptance before upload) —
    with sub[1]:
        papers = H.llm_review_queue(db)
        _curate_tab(db, papers, H.accept_llm, "tbl_llm", rows=H.llm_rows(papers))

    # — Access issues (needs_manual) —
    with sub[2]:
        papers = H.triage_papers(db)
        st.caption(f"{len(papers)} had access issues")
        sel = _curate_tab(db, papers, H.promote_links_to_ready, "tbl_triage",
                          rows=H.triage_rows(papers))
        # The dataframe selection survives the Open click (no reload), so the same
        # batch you opened is still selected when you come back to mark it added.
        o1, o2 = st.columns(2)
        if o1.button(f"🌐 Open {len(sel)} selected link(s) in browser",
                     key="open_sel_triage", disabled=not sel,
                     help="Opens the selected access-issue URLs in a new window"):
            n = H.open_urls([papers[i]["url"] for i in sel])
            st.toast(f"Opened {n} link(s) in your browser.")
        if o2.button(f"✓ Mark {len(sel)} selected as added to Zotero",
                     key="confirm_sel_triage", disabled=not sel,
                     help="After you've saved them with the Zotero connector, mark "
                          "them done. They count as Uploaded in the database."):
            _undoable(f"Marked {len(sel)} as added to Zotero")
            n = db.confirm_added_to_zotero([papers[i]["url"] for i in sel])
            db.save(); refresh_tables()
            st.toast(f"Marked {n} as added to Zotero.")
            st.rerun()

    # — Links (reachable non-papers worth keeping as webpage bookmarks) —
    with sub[3]:
        papers = H.link_papers(db)
        st.caption(f"{len(papers)} links that aren't traditionally citable")
        sel = _curate_tab(db, papers, H.promote_links_to_ready, "tbl_link",
                          rows=H.cull_rows(papers))
        _open_links_row(papers, sel, "tbl_link")

    # — Likely junk (clutter; bulk-delete) —
    with sub[4]:
        papers = H.junk_papers(db)
        st.caption(f"{len(papers)} seem to be junk")
        sel = _curate_tab(db, papers, H.promote_links_to_ready, "tbl_junk",
                          rows=H.cull_rows(papers))
        _open_links_row(papers, sel, "tbl_junk")

    # — Duplicates —
    with sub[5]:
        _render_duplicates(db)


def _render_duplicates(db):
    groups = db.find_duplicate_groups()
    if not groups:
        st.success("No duplicate groups detected.")
        return
    st.caption(f"{len(groups)} groups that share a DOI or title")
    # Keys use each paper's stable normalized URL, not a positional index:
    # index keys get reused across groups after a delete and desync the browser.
    for group in groups:
        label = group[0].get("title") or group[0].get("doi") or group[0]["url"]
        with st.expander(f"{label[:80]}  ({len(group)} URLs)", expanded=True):
            for p in group:
                cols = st.columns([6, 1])
                cols[0].markdown(f"[{p['url']}]({p['url']})")
                if cols[1].button("Keep this", key=f"keep::{normalize_url(p['url'])}"):
                    _undoable(f"Resolved duplicate (kept 1, deleted {len(group) - 1})")
                    H.delete_papers(db, [o["url"] for o in group
                                         if o["url"] != p["url"]])  # + their PDFs
                    refresh_tables(); st.rerun()


# ── Tab 4: Upload ───────────────────────────────────────────────────────────

def _do_upload(papers, db, as_links=False):
    """Shared upload handler. as_links=True recasts each item as a Zotero webpage
    item (via Citoid, title falling back to the URL) before upload."""
    if not papers:
        st.info("Nothing to upload.")
        return
    try:
        zot = zotero_client.make_zotero_client()
    except ValueError as exc:
        st.error(str(exc))
        return
    if as_links:
        rbar = st.progress(0.0, text=f"Resolving {len(papers)} link(s) via Citoid…")
        H.apply_link_metadata(db, papers, progress_cb=lambda d, t: rbar.progress(
            d / max(t, 1), text=f"Resolved {d}/{t} link(s)…"))
        rbar.empty()
    ubar = st.progress(0.0, text=f"Uploading {len(papers)} item(s)…")
    result = zotero_client.upload_papers(papers, zot, db, progress_cb=lambda d, t: ubar.progress(
        d / max(t, 1), text=f"Uploaded {d}/{t}…"))
    ubar.empty()
    # upload_papers mutated `db` (the object shared by every tab) in place and
    # saved to disk. Stash the summary and RERUN so the whole page repaints from
    # one consistent state — without the rerun, views rendered before this point
    # (status bar, Review) show the pre-upload state while the Database tab
    # (rendered after) shows the post-upload state. The in-memory db is already
    # authoritative, so refresh tables rather than re-reading from disk. See get_db().
    st.session_state["upload_flash"] = (
        f"Uploaded {result['uploaded']}/{result['total']}, "
        f"{result['pdfs_attached']} PDF(s) attached, "
        f"{result['notes_added']} Slack note(s) added, {result['failed']} failed.")
    st.session_state["upload_flash_errors"] = result["errors"]
    refresh_tables()
    st.rerun()


def _render_upload_errors(db):
    """Fixable list of papers Zotero rejected: shows the error, lets you edit the
    fields inline (auto-saved), then Retry. These are held out of Ready until they
    upload cleanly (or you fix them)."""
    errs = H.upload_error_papers(db)
    if not errs:
        return
    st.divider()
    st.markdown(f"**⚠ Upload errors ({len(errs)})** — Zotero rejected these, so "
                "they're held out of Ready. The *Error* column says why; fix the "
                "fields (saved as you type), then **Retry**.")
    edited = st.data_editor(
        _spaced(H.upload_error_rows(errs)), key=_tkey("tbl_uperr"), hide_index=True,
        width="stretch", height=_REVIEW_TABLE_H, disabled=["Error", "URL"],
        column_config=_COLS,
    )
    H.apply_table_edits(db, errs, edited)  # auto-save edits on every rerun
    if st.button("🔄 Retry upload", key="retry_uperr", type="primary"):
        _do_upload(errs, db)


def tab_upload(db):
    st.subheader("Upload to Zotero")
    flash = st.session_state.pop("upload_flash", None)
    if flash:
        st.success(flash)
    flash_errors = st.session_state.pop("upload_flash_errors", None)
    if flash_errors:
        with st.expander(f"{len(flash_errors)} error(s) from the last upload"):
            for url, msg in flash_errors:
                st.text(f"{url}\n    {msg}")
    ready = zotero_client.uploadable_papers(db)
    st.caption(f"{len(ready)} ready to upload to Zotero")

    dups = db.find_duplicate_groups()
    if dups:
        st.warning(f"{len(dups)} duplicates exist. Use the duplicates tab to resolve "
                   "these or upload anyway with duplicates.")

    if st.button("⬆ Upload all ready", type="primary", disabled=not ready):
        _do_upload(ready, db)
    st.caption("Move links to Ready to upload without metadata.")

    _render_upload_errors(db)

    # PDF-only papers (a saved PDF with no metadata, needing a manual Zotero drag-in)
    # are rare now that pypdf + vision usually extract metadata — so only show this
    # whole section when there actually are some.
    n_pdfonly = H.status_counts(db)["PDF only"]
    if n_pdfonly:
        st.divider()
        st.markdown(f"**PDF-only papers ({n_pdfonly})** — saved files with no metadata. "
                    "Save them to a folder, drag that folder into Zotero desktop (its "
                    "recognizer fills in the metadata), then click Confirm so they count "
                    "as uploaded.")
        n_pdf = len(H.pdf_only_papers(db))
        c1, c2, c3 = st.columns(3)
        if c1.button(f"💾 Save {n_pdf} PDFs to a folder…", disabled=not n_pdf):
            dest = H.finder_pick_folder()
            if dest:
                n = H.export_pdfs(db, dest)
                st.success(f"Saved {n} PDF(s) to {dest}")
        if c2.button(f"📁 Open the default folder ({n_pdf})"):
            # Rebuild the default queue from the DB first so it can't show stale files.
            H.export_pdfs(db, H.upload_queue_dir(), wipe=True)
            subprocess.run(["open", str(H.upload_queue_dir())])
        if c3.button(f"✓ Confirm {n_pdfonly} added to Zotero",
                     help="Mark the PDF-only papers as done after you've dragged them "
                          "into Zotero desktop, so the counts stay correct."):
            _undoable(f"Confirmed {n_pdfonly} PDF paper(s) as uploaded")
            n = db.confirm_pdf_uploads()
            db.save()
            cleared = H.clear_upload_queue()    # they've been dragged into Zotero — empty the staging folder
            st.session_state.upload_flash = (
                f"Confirmed {n} PDF paper(s) as uploaded"
                + (f"; cleared {cleared} file(s) from the drag-in folder." if cleared
                   else "."))
            refresh_tables()
            st.rerun()


# ── Main ────────────────────────────────────────────────────────────────────

_TAB_CSS = """
<style>
/* Top-level tabs: larger + bold so they stand out from sub-tabs. */
[data-baseweb="tab-list"] button[data-baseweb="tab"] {
    font-size: 1.25rem;
    font-weight: 700;
    padding: 0.5rem 0.25rem;
}
/* Nested sub-tabs (inside a tab panel): back to a normal, smaller size. */
[data-baseweb="tab-panel"] [data-baseweb="tab-list"] button[data-baseweb="tab"] {
    font-size: 0.9rem;
    font-weight: 400;
    padding: 0.25rem 0.1rem;
}
</style>
"""


def main():
    # Re-read .env as the authoritative source on every rerun, so a value the
    # user fixes in the file (or via the Settings form) takes effect on the next
    # refresh instead of being shadowed by a stale value left in os.environ.
    config.load_dotenv(override=True)
    db = get_db()
    st.markdown(_TAB_CSS, unsafe_allow_html=True)
    st.title("Slack to Zotero pipeline")
    render_env_banner()
    render_status_bar(db)
    render_undo_banner()
    st.divider()

    tabs = st.tabs(["1 · Ingest", "2 · Enrich", "3 · Review", "4 · Upload",
                    "📚 Database", "⚙ Settings"])
    with tabs[0]:
        tab_ingest(db)
    with tabs[1]:
        tab_enrich(db)
    with tabs[2]:
        tab_review(db)
    with tabs[3]:
        tab_upload(db)
    with tabs[4]:
        tab_database(db)
    with tabs[5]:
        tab_settings(db)

    st.divider()
    if _is_admin() and st.toggle("🛠 Debug mode", key="debug_mode"):
        render_debug(db)

    render_footer()


# ── Tab: Database ────────────────────────────────────────────────────────────

def tab_database(db):
    st.subheader("Full database")
    up, not_up = H.papers_by_upload_state(db)
    sub = st.tabs([f"Not uploaded ({len(not_up)})", f"Uploaded ({len(up)})"])

    # Not-uploaded: selectable, with two whole-record actions (no field editing,
    # so nothing here can produce malformed Zotero data).
    with sub[0]:
        st.caption("Select rows to re-enrich or delete.")
        if not not_up:
            st.info("No papers here.")
        else:
            sel = _table(H.full_rows(not_up), "tbl_db_notup")
            c1, c2 = st.columns(2)
            if sel and c1.button("↩ Reset to Pending (re-enrich)", key="db_reset",
                                 help="Wipe resolved metadata and the cached PDF, "
                                      "and send back to the start of the cascade. "
                                      "Keeps the URL and Slack comments."):
                _undoable(f"Reset {len(sel)} to Pending")
                H.reset_papers(db, [not_up[i]["url"] for i in sel])
                refresh_tables(); st.rerun()
            if sel and c2.button("🗑 Delete permanently", key="db_delete"):
                _undoable(f"Deleted {len(sel)} paper(s)")
                _delete_selected(db, not_up, sel); st.rerun()

    with sub[1]:
        _db_table(up)


def _db_table(papers):
    if not papers:
        st.info("No papers here.")
        return
    # Inline: a fixed-height container gives the table a comfortable size to
    # stretch into (plain height="stretch" collapses to its minimum on an
    # unbounded page). Fullscreen: the table lifts out of the container and
    # fills the whole window, since stretch fills the fullscreen modal.
    with st.container(height=650):
        st.dataframe(_spaced(H.full_rows(papers)), hide_index=True, width="stretch",
                     height="stretch", column_config=_COLS)


def render_footer():
    """Status line shown at the bottom of every page: the active Claude model."""
    st.divider()
    st.caption(f"Current Claude model in use: `{config.llm_model()}`")
    if config.anthropic_api_key() and _credit_status() == "no_credit":
        st.error("Anthropic API is out of credits. The LLM step fails until you add "
                 "credits at console.anthropic.com. Everything else works.")


# ── Settings ────────────────────────────────────────────────────────────────

# (env var, label, secret?) for the settings form.
_SETTINGS_FIELDS = [
    ("ZOTERO_API_KEY",       "Zotero API key",            True),
    ("ZOTERO_LIBRARY_ID",    "Zotero library ID",         False),
    ("ZOTERO_LIBRARY_TYPE",  "Zotero library type (group/user)", False),
    ("ZOTERO_COLLECTION_KEY", "Zotero collection key (optional)", False),
    ("ANTHROPIC_API_KEY",    "Anthropic (Claude) API key", True),
]


def tab_settings(db):
    st.subheader("Settings")
    # On the hosted deploy, secrets live in the Streamlit dashboard (bridged into
    # os.environ by config.load_streamlit_secrets), NOT in .env — which is on the
    # cloud's ephemeral filesystem and resets on every reboot. So the in-app secrets
    # form there is both non-persistent and a way to expose the lab's shared keys to
    # every viewer; hide it and point to the dashboard owner. TURSO_DATABASE_URL is
    # only set on the hosted deploy (see .env.example), so it's our "is hosted" flag.
    if os.environ.get("TURSO_DATABASE_URL"):
        st.info("Secrets are hosted by the Streamlit dashboard. Contact "
                "david.beeson123@gmail.com (David Beeson) if you need to change "
                "anything.")
        _admin_token = os.environ.get("ADMIN_TOKEN", "")
        if _admin_token:
            if st.session_state.get("_admin_unlocked"):
                st.caption("Admin access active for this session.")
                if st.button("Lock", key="admin_lock"):
                    st.session_state._admin_unlocked = False
                    st.rerun()
            else:
                _tok = st.text_input("Admin token", type="password",
                                     key="admin_token_input",
                                     placeholder="Enter token to unlock debug mode")
                if _tok and _tok == _admin_token:
                    st.session_state._admin_unlocked = True
                    st.rerun()
    else:
        st.caption("Saved to the `.env` file and applied immediately. Leave a field "
                   "blank to keep its current value. Keys are stored in plain text in "
                   "`.env` (same as before) — keep that file private.")
        with st.form("settings_form"):
            new = {}
            for env, label, secret in _SETTINGS_FIELDS:
                current = os.environ.get(env, "")
                new[env] = st.text_input(label, value=current,
                                         type="password" if secret else "default",
                                         help=f"Environment variable: {env}")
            if st.form_submit_button("💾 Save settings", type="primary"):
                changed = config.update_env(new)
                if changed:
                    st.success(f"Saved to {config.PROJECT_ROOT / '.env'} — "
                               f"updated: {', '.join(changed)}.")
                else:
                    st.info("No changes written — every field already matched the "
                            "saved values. (Blank fields are left as-is; to *clear* "
                            "a value, edit `.env` directly.)")

    _backup_section(db)
    _model_health()


def _backup_section(db):
    """Download the whole library as a standalone SQLite file. On the hosted
    deploy the live data lives in Turso, so this is the user's insurance against a
    Turso outage — keep periodic copies. The downloaded file IS a ready-to-use
    `zotero_database.db`: drop it in locally and it opens as-is."""
    st.divider()
    st.markdown("**Backup**")
    stats = db.stats()
    st.caption(f"Download a complete copy of the library "
               f"({stats['total']} papers) as a portable SQLite file. Keep "
               "regular backups — on the cloud, Turso is the only data home.")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        data = db.export_sqlite_bytes()
    except Exception as exc:
        st.error(f"Couldn't build the backup: {exc}")
        return
    st.download_button(
        "⬇️ Download a backup copy (.db)", data=data,
        file_name=f"zotero_database-backup-{ts}.db",
        mime="application/vnd.sqlite3")


def _model_health():
    """The single place to set the Claude model: shows whether the current one is
    still available and lets the user switch. Editable dropdown — pick a current
    model, or type any model ID the API hasn't listed (the recovery path when a
    model is retired). This is why LLM_MODEL is *not* in the settings form above."""
    st.divider()
    st.markdown("**Claude model**")
    key = config.anthropic_api_key()
    model = config.llm_model()

    if key:
        if st.button("🔄 Check availability now"):
            _model_status(force=True)
        status = _model_status()
        if status == "ok":
            st.success(f"`{model}` is available.")
        elif status == "unavailable":
            st.error(f"`{model}` is no longer available (deprecated/retired). "
                     "Choose a current model below.")
        elif status == "uncheckable":
            st.caption(f"Couldn't verify `{model}` (offline, or the installed "
                       "Anthropic SDK has no models API). It may still work.")
    else:
        st.caption("Add an Anthropic API key above and save to check availability "
                   "and list current models. You can still set a model ID below.")

    # Editable picker: current models from the API (if any), the active model
    # always present and selected. accept_new_options lets the user type a model
    # ID the dropdown doesn't offer, so this one control covers every case.
    options = H.available_models(key) if key else []
    if model not in options:
        options = [model] + options
    choice = st.selectbox(
        "Model ID", options, index=options.index(model), accept_new_options=True,
        help="Pick a current model, or type any model ID the dropdown doesn't list.",
    )
    if st.button("Use this model", disabled=(choice == model)):
        config.set_llm_model(choice)   # persists to the DB (survives restarts; no
        _model_status(force=True)      # dashboard needed) and updates this process
        st.success(f"Model set to `{choice}`.")
        st.rerun()


def _run_debug(db, action):
    """Run a reset, stash the result for after the rerun, and rerun so the
    status-bar counts refresh immediately."""
    st.session_state.dbg_result = H.debug_reset(db, action)
    reload_db()
    st.rerun()


def render_debug(db):
    flash = st.session_state.pop("dbg_result", None)
    if flash:
        st.success(flash)
    st.warning("Debug tools — these modify the database directly. For testing only.")
    st.caption("Common resets:")
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("↻ Reset uploads", key="dbg_up",
                 help="Clear Zotero upload state so papers can be re-uploaded."):
        _run_debug(db, "uploaded")
    if c2.button("↻ Reset ingest", key="dbg_ing",
                 help="Clear the processed-files registry so Slack files re-ingest."):
        _run_debug(db, "processed_files")
    if c3.button("↻ Reset all enrichment", key="dbg_enr",
                 help="Send every paper back to Pending (re-runs the cascade)."):
        _run_debug(db, "enrich_all")
    if c4.button("↩ Undo Review changes", key="dbg_revert", disabled=not H.has_baseline(db),
                 help="Restore the DB to its state just after the last enrichment — "
                      "undoes all Review-tab edits, deletes, and reclassifications."):
        _run_debug(db, "revert_review")
    st.caption("Reset one stage:")
    choice = st.selectbox("Stage", list(H.DEBUG_ACTIONS), key="dbg_action",
                          label_visibility="collapsed")
    action = H.DEBUG_ACTIONS[choice]
    ok = st.text_input("Type YES to confirm", key="dbg_confirm") == "YES" \
        if action == "wipe_all" else True
    if st.button("Run reset", type="primary", disabled=not ok, key="dbg_run"):
        _run_debug(db, action)

    st.caption("Force re-ingest — re-parse files even if already processed. Use this "
               "to repopulate after the database was emptied or got out of sync with "
               "the processed-files registry (e.g. after a merge). Safe: papers and "
               "comments still dedup, so nothing duplicates.")
    if st.button("📁 Pick files & force re-ingest", key="dbg_reingest"):
        paths = H.finder_pick_files()
        if paths:
            with st.spinner(f"Re-ingesting {len(paths)} file(s)…"):
                r = H.ingest_paths(db, paths, force=True)
            slack_note = (f" Found {r['slack_files']} Slack file(s) — they'll be read "
                          "when you Enrich.") if r.get("slack_files") else ""
            st.session_state.dbg_result = (
                f"Force re-ingested: added {r['new_papers']} new paper(s) and "
                f"{r['new_comments']} comment(s) from {r['files']} file(s)." + slack_note)
            reload_db()
            st.rerun()


if __name__ == "__main__":
    main()
