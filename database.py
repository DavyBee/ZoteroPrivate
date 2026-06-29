"""
database.py — SQLite-backed storage for the Slack-to-Zotero pipeline.

A single SQLite file (zotero_database.db) holds two tables:
    papers           — one row per Paper object (the human-editable thing)
    processed_files  — bookkeeping: which Slack JSONs have been ingested

The model is still "load everything into memory, operate in memory, save it all
back" — exactly like the old JSON store — so every Database method works on the
in-memory dicts and only load()/save() touch the disk. Each paper's full dict is
kept verbatim in a `data` JSON column (the source of truth, so new Paper fields
never need a schema migration); the other columns are readable duplicates for
browsing the .db with any SQLite tool.

Writes happen inside a single transaction (atomic commit/rollback), the SQLite
equivalent of the old atomic file replace. Papers are deduped by normalized URL
or DOI; comments are deduped by Slack ts (which is globally unique).

The connection is opened in one place — _connect() — which is the ONLY spot that
changes when swapping the local file for hosted Turso (see WEBSITE_TODO.md).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, TypedDict

import pdf_storage


# ── Errors ────────────────────────────────────────────────────────────────────

class DatabaseError(Exception):
    """The database file on disk couldn't be read.

    The .db is meant to be inspectable with any SQLite tool, and each paper's
    `data` column holds JSON that could in principle be hand-edited, so a
    corrupt file or a stray malformed value is a plausible (if rare) error. We
    surface it with the path and a recovery hint rather than letting a raw
    sqlite3 / JSONDecodeError bubble up as a traceback. The bad file is never
    modified, so the data is still recoverable (restore the baseline.db
    checkpoint, or open it in a SQLite tool and fix it by hand).
    """

    def __init__(self, path: str, original: Exception):
        self.path = path
        self.original = original
        super().__init__(
            f"{path} could not be read as a SQLite database ({original}). The "
            "file was left untouched. Restore it from state/baseline.db, open "
            "it in a SQLite tool to repair it, or move the file aside to start "
            "fresh."
        )


# ── Types ─────────────────────────────────────────────────────────────────────

class Comment(TypedDict):
    author: str          # display name
    ts: str              # Slack message ts (globally unique per workspace)
    is_reply: bool       # True if this is a thread reply
    text: str            # cleaned message text


class Paper(TypedDict, total=False):
    # Identity
    url: str
    doi: Optional[str]

    # Bibliographic metadata (filled in by metadata.py)
    title: Optional[str]
    authors: list[str]
    abstract: Optional[str]
    year: Optional[str]
    journal: Optional[str]
    item_type: str       # Zotero type: journalArticle, preprint, report, etc.
    keywords: list[str]
    volume: Optional[str]
    issue: Optional[str]
    pages: Optional[str]
    publisher: Optional[str]
    isbn: Optional[str]
    language: Optional[str]
    # LLM's 1-2 sentence read of what the source is/shows — a culling aid for the
    # Links / Likely-junk / Access-issues queues, not uploaded to Zotero.
    summary: Optional[str]
    # LLM's paper/link/junk guess (the routing category), persisted as a triage
    # aid — esp. in Access issues, where we often have no title to go on.
    llm_category: Optional[str]
    # True when the LLM had little/no extracted page text (it guessed from the
    # URL + Slack comments) — flagged in the review tables as "verify".
    low_context: Optional[bool]
    # Set when this paper came from a Slack file upload: the url is the file's
    # Slack permalink (not a web URL), slack_file_url is the url_private download
    # link (carries an expiring token), slack_file_name is the original filename.
    # The Enrich tab's "Download Slack PDFs" step fetches the bytes. None for
    # URL-shared papers.
    slack_file_url: Optional[str]
    slack_file_name: Optional[str]

    # Slack context
    comments: list[Comment]

    # Local PDF (saved by pdf_storage.save_pdf when the URL yields a real PDF)
    local_pdf_path: Optional[str]
    pdf_size_bytes: Optional[int]

    # Metadata fetch state. metadata_source values:
    #   "none"         — pipeline hasn't run yet
    #   "pdf_saved"    — we have the file; Zotero will recognize it on import
    #   "translator"   — the Zotero translator engine (Citoid) returned full
    #                    metadata (also reused for human-promoted link uploads)
    #   "crossref_doi" — CrossRef DOI lookup returned full metadata
    #   "llm"          — Claude returned full or useful-partial metadata
    #   "needs_manual" — a citable work we couldn't retrieve (paywall, bot
    #                    block, dead/empty page), OR a non-paper we never saw;
    #                    flagged for human review ("Access issues" in the app)
    #   "link"         — reachable but not a directly-citable paper, yet worth
    #                    keeping as a saved webpage (tweet, profile/landing page,
    #                    journal TOC, Drive folder); "Links" in the app
    #   "non_paper"    — reachable clutter with no place in a library (reaction
    #                    GIF, dead/parked page, bare homepage); "Likely junk"
    metadata_source: str
    metadata_fetched_at: Optional[str]
    last_error: Optional[str]
    attempts: int

    # Zotero upload state
    zotero_key: Optional[str]
    uploaded_at: Optional[str]
    # Set when Zotero rejected this item on upload (the API error message). Holds
    # the paper out of Ready and into the "Upload error" state so the lab member
    # can fix it in the Upload tab and retry. Cleared on a successful upload.
    upload_error: Optional[str]
    # Set when the lab member added this paper to Zotero by hand via the browser
    # connector (the Access-issues path). Like pdf_confirmed, it makes the paper
    # count as Uploaded so it leaves the queue. Cleared by reset_uploaded_state.
    manually_added: Optional[bool]

    # Housekeeping
    first_seen_at: str


class ProcessedFile(TypedDict):
    hash: str
    processed_at: str
    papers_found: int
    messages_processed: int


# ── URL normalization & hashing ───────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """Lowercase scheme + host, strip trailing slash; preserve path case."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    host, *path = rest.split("/", 1)
    return f"{scheme.lower()}://{host.lower()}" + ("/" + path[0] if path else "")


def hash_file(path: str) -> str:
    """SHA-256 hex of a file's contents. Used for processed-file dedup."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SQLite connection & schema ────────────────────────────────────────────────

# Readable duplicate columns mirrored out of each paper's `data` JSON, in the
# exact order they're inserted. `key` is the normalized URL (the in-memory dict
# key); `data` is the full Paper dict and the source of truth.
_PAPER_COLUMNS = (
    "key", "url", "title", "doi", "year", "journal", "item_type",
    "authors", "metadata_source", "zotero_key", "uploaded_at",
    "upload_error", "data",
)

# One statement per entry (not a single executescript blob): the libsql/Turso
# client implements the core sqlite3 API but not necessarily executescript, so
# we run statements individually to keep the local and hosted paths identical.
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS papers (
        key             TEXT PRIMARY KEY,
        url             TEXT,
        title           TEXT,
        doi             TEXT,
        year            TEXT,
        journal         TEXT,
        item_type       TEXT,
        authors         TEXT,
        metadata_source TEXT,
        zotero_key      TEXT,
        uploaded_at     TEXT,
        upload_error    TEXT,
        data            TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_files (
        filename           TEXT PRIMARY KEY,
        hash               TEXT,
        processed_at       TEXT,
        papers_found       INTEGER,
        messages_processed INTEGER
    )
    """,
    # Durable key/value app config (currently just LLM_MODEL). Lives in the DB so a
    # model choice — picked in-app OR auto-selected when a model is retired —
    # survives restarts on the cloud's ephemeral filesystem, with no dashboard /
    # maintainer needed. See config.load_persisted_model / config.set_llm_model.
    """
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
)


def _connect(path: str) -> sqlite3.Connection:
    """Open a connection to the database. This is the ONLY place a connection is
    created, so swapping the local SQLite file for hosted Turso (Phase 3, see
    WEBSITE_TODO.md) touches just this function — everything else speaks the
    DB-API the returned object provides.

    A Turso URL/token in the environment will be branched on here; absent that
    (all local dev), it's plain stdlib sqlite3 against a file on disk.
    """
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_AUTH_TOKEN")
    if turso_url and turso_token:
        # Hosted SQLite (Turso) via the libsql client, which exposes the same
        # DB-API surface (execute/executemany/commit/close, `with conn:`
        # transactions) as stdlib sqlite3, so the rest of this module is
        # unchanged. Imported lazily so local dev needs neither the package nor
        # a Turso account.
        import libsql                       # type: ignore
        return libsql.connect(turso_url, auth_token=turso_token)
    conn = sqlite3.connect(path)
    return conn


def _ensure_schema(conn) -> None:
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.commit()


# ── Durable key/value settings ────────────────────────────────────────────────
# Standalone (not on the Database instance) so callers without a loaded Database —
# config at startup, the LLM self-heal in metadata — can read/write a setting with
# one call. They open their own connection via _connect, so they hit Turso on the
# hosted deploy exactly like everything else. The DB path comes from config (late
# import: database itself does not import config, so there's no cycle).

def _settings_conn():
    import config
    return _connect(str(config.DB_PATH))


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a persisted setting. Never raises — returns `default` if the table,
    row, or database isn't reachable (e.g. first run, or Turso briefly down)."""
    try:
        conn = _settings_conn()
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row and row[0] is not None else default
        finally:
            conn.close()
    except Exception:
        return default


def set_setting(key: str, value: str) -> None:
    """Persist a setting. DELETE+INSERT rather than UPSERT so the local sqlite3
    and hosted libsql paths behave identically (same reason as save())."""
    conn = _settings_conn()
    try:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def _paper_row(key: str, p: "Paper") -> tuple:
    """Flatten a Paper into the papers-table row: the browsable duplicate
    columns plus the full dict as JSON in `data`."""
    authors = p.get("authors") or []
    authors_str = "; ".join(authors) if isinstance(authors, list) else str(authors)
    return (
        key,
        p.get("url"),
        p.get("title"),
        p.get("doi"),
        p.get("year"),
        p.get("journal"),
        p.get("item_type"),
        authors_str,
        p.get("metadata_source"),
        p.get("zotero_key"),
        p.get("uploaded_at"),
        p.get("upload_error"),
        json.dumps(p, ensure_ascii=False),
    )


# ── Database ──────────────────────────────────────────────────────────────────

class Database:
    """
    On disk: a single SQLite file with `papers` and `processed_files` tables.
    In memory: papers are a dict keyed by normalized URL for O(1) lookup, and
    processed-files a dict keyed by filename. Convert at load/save boundaries
    only — every other method operates purely in memory.

    `processed_path` is accepted for backwards compatibility with the old
    two-file (JSON) layout but is unused: both tables now live in `db_path`.
    """

    def __init__(self, db_path: str, processed_path: str):
        self.db_path = db_path
        self.processed_path = processed_path
        self._papers: dict[str, Paper] = {}
        self._processed: dict[str, ProcessedFile] = {}

    @classmethod
    def load(cls, db_path: str, processed_path: str) -> "Database":
        db = cls(db_path, processed_path)
        conn = _connect(db_path)
        try:
            _ensure_schema(conn)
            try:
                paper_rows = conn.execute("SELECT data FROM papers").fetchall()
                file_rows = conn.execute(
                    "SELECT filename, hash, processed_at, papers_found, "
                    "messages_processed FROM processed_files"
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                raise DatabaseError(db_path, exc) from exc
            for (data,) in paper_rows:
                try:
                    p = json.loads(data)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise DatabaseError(db_path, exc) from exc
                db._papers[normalize_url(p["url"])] = p
            for fn, h, at, found, msgs in file_rows:
                db._processed[fn] = {
                    "hash": h,
                    "processed_at": at,
                    "papers_found": found,
                    "messages_processed": msgs,
                }
        finally:
            conn.close()
        return db

    def save(self) -> None:
        conn = _connect(self.db_path)
        try:
            _ensure_schema(conn)
            # The whole rewrite runs as one transaction and is committed at the
            # end, so a crash mid-save leaves the previous contents intact (the
            # old atomic-replace guarantee). Explicit commit/rollback rather than
            # `with conn:` so the local sqlite3 and hosted libsql paths behave
            # identically (libsql doesn't implement the context-manager form).
            try:
                conn.execute("DELETE FROM papers")
                conn.executemany(
                    f"INSERT INTO papers ({', '.join(_PAPER_COLUMNS)}) "
                    f"VALUES ({', '.join('?' * len(_PAPER_COLUMNS))})",
                    [_paper_row(k, p) for k, p in self._papers.items()],
                )
                conn.execute("DELETE FROM processed_files")
                conn.executemany(
                    "INSERT INTO processed_files (filename, hash, processed_at, "
                    "papers_found, messages_processed) VALUES (?, ?, ?, ?, ?)",
                    [
                        (fn, r.get("hash"), r.get("processed_at"),
                         r.get("papers_found"), r.get("messages_processed"))
                        for fn, r in self._processed.items()
                    ],
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        finally:
            conn.close()

    def export_sqlite_bytes(self) -> bytes:
        """Serialize the current in-memory state to a standalone SQLite file and
        return its raw bytes — a portable, ready-to-use backup of the library.

        Always builds a plain local stdlib-sqlite3 file (never routes through
        _connect), so on the hosted deploy this captures a downloadable snapshot
        of the Turso-backed data: drop the downloaded file in as
        zotero_database.db and it loads with no further conversion. Insurance
        against a Turso outage (see WEBSITE_TODO.md 4.3)."""
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            try:
                _ensure_schema(conn)
                conn.executemany(
                    f"INSERT INTO papers ({', '.join(_PAPER_COLUMNS)}) "
                    f"VALUES ({', '.join('?' * len(_PAPER_COLUMNS))})",
                    [_paper_row(k, p) for k, p in self._papers.items()],
                )
                conn.executemany(
                    "INSERT INTO processed_files (filename, hash, processed_at, "
                    "papers_found, messages_processed) VALUES (?, ?, ?, ?, ?)",
                    [
                        (fn, r.get("hash"), r.get("processed_at"),
                         r.get("papers_found"), r.get("messages_processed"))
                        for fn, r in self._processed.items()
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            with open(path, "rb") as f:
                return f.read()
        finally:
            os.remove(path)

    # ── Paper operations ──────────────────────────────────────────────────────

    def add_paper(self, url: str, doi: Optional[str] = None) -> tuple[str, bool]:
        """
        Add a paper if not already present. Dedupes by normalized URL and (when
        a DOI is given) by DOI. Returns (canonical_key, is_new). The canonical
        key may be a different URL than the input if a DOI match merged this
        URL into an existing paper — pass that key to subsequent calls.
        """
        key = normalize_url(url)
        if key in self._papers:
            return key, False
        if doi:
            doi_norm = doi.lower().strip()
            for k, p in self._papers.items():
                existing = p.get("doi")
                if existing and existing.lower().strip() == doi_norm:
                    return k, False
        self._papers[key] = _new_paper(url, doi)
        return key, True

    def get_paper(self, url: str) -> Optional[Paper]:
        return self._papers.get(normalize_url(url))

    def update_paper(self, key: str, **fields) -> None:
        """Update a paper by its canonical key (from add_paper)."""
        if key not in self._papers:
            raise KeyError(f"No paper at key {key}")
        self._papers[key].update(fields)

    def add_comment(self, key: str, comment: Comment) -> bool:
        """
        Append comment to the paper at `key`. Returns True if added, False if
        a comment with the same Slack ts already exists (idempotent re-ingest).
        """
        if key not in self._papers:
            raise KeyError(f"No paper at key {key}")
        existing_ts = {c["ts"] for c in self._papers[key].get("comments", [])}
        if comment["ts"] in existing_ts:
            return False
        self._papers[key].setdefault("comments", []).append(comment)
        return True

    def mark_uploaded(self, key: str, zotero_key: str) -> None:
        self.update_paper(key, zotero_key=zotero_key, uploaded_at=_now(),
                          upload_error=None)

    def all_papers(self) -> list[Paper]:
        return list(self._papers.values())

    def unenriched_papers(self) -> list[Paper]:
        """Papers whose metadata pipeline hasn't yielded anything yet — including
        Slack file uploads (enrich_paper downloads + LLM-reads those as part of
        the normal Enrich run)."""
        return [p for p in self._papers.values()
                if p.get("metadata_source", "none") == "none"]

    def pending_upload(self) -> list[Paper]:
        """Papers with metadata but not yet pushed to Zotero."""
        return [p for p in self._papers.values()
                if p.get("title") and not p.get("zotero_key")]

    def find_duplicate_groups(self) -> list[list[Paper]]:
        """
        Detect duplicate papers by DOI match, then normalized-title match for
        papers without a DOI. Returns a list of groups (each group is 2+
        papers that appear to be the same work). Does not modify the DB —
        resolution is handled by the GUI (user picks the keeper and deletes
        the rest via delete_paper).

        A tweet URL is only ever a POINTER to a work — the paper it links to is
        recovered as its own record by expand_tweets — so a tweet is never a
        citable duplicate of that paper and is skipped here. (Some older tweet
        records still carry stale title/DOI from before they were blanked; this
        keeps them from colliding with the real paper in the Duplicates tab.)
        """
        from url_expander import is_tweet_url

        seen_in_group: set[str] = set()
        groups: list[list[Paper]] = []

        # Pass 1: DOI collisions
        by_doi: dict[str, list[Paper]] = {}
        for p in self._papers.values():
            if is_tweet_url(p.get("url", "")):
                continue
            doi = (p.get("doi") or "").strip().lower()
            if doi:
                by_doi.setdefault(doi, []).append(p)
        for doi, group in by_doi.items():
            if len(group) >= 2:
                groups.append(group)
                for p in group:
                    seen_in_group.add(normalize_url(p["url"]))

        # Pass 2: normalized-title collisions for papers not already grouped
        by_title: dict[str, list[Paper]] = {}
        for p in self._papers.values():
            if is_tweet_url(p.get("url", "")):
                continue
            if normalize_url(p["url"]) in seen_in_group:
                continue
            title = (p.get("title") or "").strip().lower()
            # Normalize: remove punctuation, collapse whitespace
            import re as _re
            title = _re.sub(r"[^\w\s]", "", title)
            title = _re.sub(r"\s+", " ", title).strip()
            if title:
                by_title.setdefault(title, []).append(p)
        for title, group in by_title.items():
            if len(group) >= 2:
                groups.append(group)

        return groups

    def delete_paper(self, url: str) -> bool:
        """Remove a paper from the DB by URL, along with its downloaded PDF
        (canonical store + upload-queue copy). Returns True if it existed."""
        key = normalize_url(url)
        if key in self._papers:
            pdf_storage.delete_local_pdfs(self._papers[key].get("local_pdf_path"))
            del self._papers[key]
            self.save()
            return True
        return False

    def upgradeable_papers(self) -> list[Paper]:
        """
        LLM-sourced papers that carry a DOI. Candidates for re-running the
        identifier lookup (CrossRef) on, since CrossRef produces more reliable,
        Zotero-aligned output than the LLM's own field extraction. Catches both:
          - title-less partials (LLM had a DOI but couldn't parse the rest)
          - full LLM records whose data may be incomplete or noisy
        """
        return [p for p in self._papers.values()
                if p.get("metadata_source") == "llm" and p.get("doi")]

    # ── Processed-files operations ────────────────────────────────────────────

    def is_file_processed(self, filename: str, content_hash: str) -> bool:
        rec = self._processed.get(filename)
        return bool(rec and rec.get("hash") == content_hash)

    def mark_file_processed(self, filename: str, content_hash: str,
                            papers_found: int, messages_processed: int) -> None:
        self._processed[filename] = {
            "hash": content_hash,
            "processed_at": _now(),
            "papers_found": papers_found,
            "messages_processed": messages_processed,
        }

    def clear_all_papers(self) -> int:
        """Delete every paper record from the DB, along with each paper's
        downloaded PDF (canonical store + upload-queue copy). Irreversible."""
        n = len(self._papers)
        for p in self._papers.values():
            pdf_storage.delete_local_pdfs(p.get("local_pdf_path"))
        self._papers = {}
        return n

    def reset_processed_files(self) -> int:
        """Clear the processed-files registry so all files will be re-ingested."""
        n = len(self._processed)
        self._processed = {}
        return n

    def reset_enrichment(self, states: Optional[set[str]] = None,
                         include_errored: bool = False) -> int:
        """Reset papers to unenriched state, preserving URL and comments.

        states=None resets every enriched paper (the default). Pass a set of
        metadata_source values to reset only those buckets; set include_errored
        to also reset any paper carrying a last_error. Returns the count reset.
        """
        n = 0
        for p in self._papers.values():
            src = p.get("metadata_source", "none")
            if states is None:
                hit = src != "none"
            else:
                hit = src in states or (include_errored and bool(p.get("last_error")))
            if not hit:
                continue
            _reset_paper_to_pending(p)
            n += 1
        return n

    def reset_paper(self, url: str) -> bool:
        """Reset a single paper to Pending (clears resolved metadata, keeps the
        URL and Slack comments) so the next Enrich re-runs the full cascade on
        it. Returns True if the paper existed. Does not save — caller saves."""
        p = self._papers.get(normalize_url(url))
        if p is None:
            return False
        _reset_paper_to_pending(p)
        return True

    def reset_uploads(self) -> int:
        """Clear Zotero upload state (zotero_key / uploaded_at / pdf_confirmed /
        manually_added) so papers can be re-uploaded. Does not touch metadata.
        Returns count."""
        n = 0
        for p in self._papers.values():
            if (p.get("zotero_key") or p.get("uploaded_at") or p.get("pdf_confirmed")
                    or p.get("upload_error") or p.get("manually_added")):
                p["zotero_key"] = None
                p["uploaded_at"] = None
                p["pdf_confirmed"] = None
                p["upload_error"] = None
                p["manually_added"] = None
                n += 1
        return n

    def confirm_pdf_uploads(self) -> int:
        """Mark every pdf_saved paper as manually added to Zotero (the lab member
        dragged the PDF folder into Zotero desktop). Sets pdf_confirmed so these
        count as Uploaded instead of lingering in 'PDF only'. Returns count."""
        n = 0
        for p in self._papers.values():
            if p.get("metadata_source") == "pdf_saved" and not p.get("pdf_confirmed"):
                p["pdf_confirmed"] = True
                p["uploaded_at"] = _now()
                n += 1
        return n

    def confirm_added_to_zotero(self, urls: list[str]) -> int:
        """Mark the given papers as manually added to Zotero via the browser
        connector — the Access-issues equivalent of confirm_pdf_uploads, but for a
        selected subset so the lab member can close the loop in batches. Sets
        manually_added so they count as Uploaded and leave the queue. Already-
        uploaded papers are left alone. Returns count newly confirmed."""
        n = 0
        for url in urls:
            p = self._papers.get(normalize_url(url))
            if p is not None and not p.get("manually_added") and not p.get("zotero_key"):
                p["manually_added"] = True
                p["uploaded_at"] = _now()
                n += 1
        return n

    def reset_tweet_expansion(self) -> int:
        """Clear the tweet_expanded / tweet_target flags so tweet URLs can be
        re-expanded (e.g. after a syndication-endpoint outage). Returns count."""
        n = 0
        for p in self._papers.values():
            if p.get("tweet_expanded") or p.get("tweet_target"):
                p["tweet_expanded"] = None
                p["tweet_target"] = None
                n += 1
        return n

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        papers = self.all_papers()
        def src(p): return p.get("metadata_source", "none")
        pdf_bytes = sum(p.get("pdf_size_bytes") or 0 for p in papers)
        return {
            "total":             len(papers),
            "pdfs_saved":        sum(1 for p in papers if src(p) == "pdf_saved"),
            "pdf_disk_mb":       round(pdf_bytes / (1024 * 1024), 1),
            "with_metadata":     sum(1 for p in papers if src(p) in
                                     ("translator", "crossref_doi", "llm")),
            "needs_manual":      sum(1 for p in papers if src(p) == "needs_manual"),
            "links":             sum(1 for p in papers if src(p) == "link"),
            "non_papers":        sum(1 for p in papers if src(p) == "non_paper"),
            "uploaded":          sum(1 for p in papers if p.get("zotero_key")),
            "pending_upload":    sum(1 for p in papers
                                     if p.get("title") and not p.get("zotero_key")),
            "needing_metadata":  sum(1 for p in papers if src(p) == "none"),
            "errored":           sum(1 for p in papers if p.get("last_error")),
            "files_processed":   len(self._processed),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reset_paper_to_pending(p: Paper) -> None:
    """Wipe all resolved metadata so the cascade re-runs from scratch, keeping
    only identity (url) and Slack context (comments). Shared by
    reset_enrichment (bulk) and reset_paper (single).

    The cached PDF is deleted too (canonical store + upload-queue copy): a paper
    reset to Pending will re-download on the next Enrich, so keeping the old file
    only orphans it on disk."""
    pdf_storage.delete_local_pdfs(p.get("local_pdf_path"))
    for f in ("title", "authors", "abstract", "year", "journal",
              "keywords", "volume", "issue", "pages", "publisher",
              "isbn", "language", "doi", "metadata_fetched_at",
              "last_error", "local_pdf_path", "pdf_size_bytes", "upload_error"):
        p[f] = [] if f in ("authors", "keywords") else None
    p["item_type"] = "journalArticle"
    p["metadata_source"] = "none"
    p["attempts"] = 0


def _new_paper(url: str, doi: Optional[str]) -> Paper:
    return {
        "url": url,
        "doi": doi,
        "title": None,
        "authors": [],
        "abstract": None,
        "year": None,
        "journal": None,
        "item_type": "journalArticle",
        "keywords": [],
        "volume": None,
        "issue": None,
        "pages": None,
        "publisher": None,
        "isbn": None,
        "language": None,
        "comments": [],
        "local_pdf_path": None,
        "pdf_size_bytes": None,
        "metadata_source": "none",
        "metadata_fetched_at": None,
        "last_error": None,
        "attempts": 0,
        "zotero_key": None,
        "uploaded_at": None,
        "upload_error": None,
        "first_seen_at": _now(),
    }
