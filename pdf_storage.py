"""
pdf_storage.py — Best-effort PDF download + local storage.

This is the new first stage of the metadata pipeline. The premise: if we can
get the PDF locally, the lab member imports it into Zotero desktop and lets
Zotero's "Retrieve Metadata for PDF" recognizer fill in everything. We don't
have to extract metadata ourselves — we just have to land the file.

What `save_pdf(url)` tries, in order:
    1. GET the URL. If the response is a real PDF (Content-Type or %PDF
       magic-byte check), save it. Done.
    2. If the response is HTML, scan it for a `<meta name="citation_pdf_url">`
       tag — a Google-Scholar / Zotero standard that most major academic
       publishers (Wiley, Nature, Science, JAMA, BMJ, Cell, etc.) include in
       their landing-page HTML. Follow that single redirect, fetch, save.
    3. Anything else (paywall HTML stub, bot block, JS-rendered link, error)
       → return None and let the caller fall through to the metadata cascade.

We never chain meta-tag follows past depth 1; we never run JavaScript; we
never share browser cookies. This keeps the dependency surface tiny and the
failure modes obvious. More elaborate strategies (Unpaywall, headless
browser) are intentionally future work.

Design notes:
- Files are saved as `pdfs/<sha256-of-normalized-url>[..16].pdf`. Hash-based
  filenames give us deduplication for free and avoid filesystem-illegal
  characters in URLs.
- The `%PDF` magic-byte check is the single most important defense against
  saving HTML paywall stubs that some publishers serve in place of the PDF.
- Size cap (config.PDF_MAX_BYTES) prevents a single oversized PDF from
  filling the disk. Hard read cap (config.PDF_FETCH_BYTES) protects against
  servers that ignore Range/length headers entirely.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import config
import http_fetch


# ── Public entry point ────────────────────────────────────────────────────────

def save_pdf(url: str) -> Optional[tuple[str, int]]:
    """
    Try to download and locally save a PDF for `url`. Returns (path, size_in_
    bytes) on success, or None if no PDF could be obtained.

    Only saves to pdfs/ — the caller (enrich_paper) decides whether to also
    copy to pdfs_to_upload/ depending on the final metadata_source outcome.

    Idempotent: if the target file already exists on disk and is a valid
    PDF, returns its path immediately without re-downloading.
    """
    config.PDFS_DIR.mkdir(parents=True, exist_ok=True)

    target = _target_path_for(url)
    if target.exists() and _looks_like_pdf_file(target):
        return (str(target), target.stat().st_size)

    # Pass 1: fetch the URL itself
    body, content_type = _fetch(url)
    if body is None:
        return None

    if _is_pdf_payload(content_type, body):
        return _write_if_within_limit(target, body)

    # Pass 2: if we got HTML, look for citation_pdf_url and try that once
    if _looks_like_html(content_type, body):
        pdf_url = _extract_citation_pdf_url(body, base=url)
        if pdf_url:
            body2, content_type2 = _fetch(pdf_url)
            if body2 is not None and _is_pdf_payload(content_type2, body2):
                return _write_if_within_limit(target, body2)

    return None


def delete_local_pdfs(local_pdf_path: Optional[str]) -> int:
    """Remove a paper's downloaded PDF — the canonical pdfs/<hash>.pdf AND any
    copy sitting in pdfs_to_upload/ — when the paper is deleted or reset.

    Filenames are hash-of-URL, so each paper owns a distinct file; removing one
    paper's PDF can never affect another's. No-op (returns 0) when there's no
    path or the files are already gone. Returns the number of files removed.
    """
    if not local_pdf_path:
        return 0
    src = Path(local_pdf_path)
    queue_copy = config.PROJECT_ROOT / "pdfs_to_upload" / src.name
    removed = 0
    for target in (src, queue_copy):
        try:
            if target.is_file():
                target.unlink()
                removed += 1
        except OSError:
            pass
    return removed


# Slack-uploaded document types we accept (paper manuscripts). Images, slide
# decks, and spreadsheets are deliberately excluded — they aren't papers.
_SLACK_DOC_EXTS = (".pdf", ".docx", ".doc", ".rtf", ".odt")


def _slack_doc_ext(filename: str) -> str:
    """The original extension if it's a supported document type, else '.pdf'."""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
        if ext in _SLACK_DOC_EXTS:
            return ext
    return ".pdf"


def save_slack_file(download_url: str, key_url: str,
                    filename: str = "") -> Optional[tuple[str, int]]:
    """Download a Slack-uploaded document (PDF or Word/RTF/ODT) from the
    token-bearing url_private, stored keyed by `key_url` (the stable permalink)
    and KEEPING the original extension so Zotero handles the file correctly when
    the lab member drags it in.

    PDFs get the %PDF magic-byte check. Other document types are accepted unless
    the response is HTML — Slack serves the real file via its token, so the only
    thing to reject is a login/error page (i.e. an expired token). Size cap and
    idempotency (on key_url) as for save_pdf. Returns (path, size) or None.
    (Note: Zotero's metadata recognizer is PDF-only, so non-PDF docs land in the
    drag-in folder as a fallback — see metadata.enrich_local_file.)"""
    config.PDFS_DIR.mkdir(parents=True, exist_ok=True)
    ext = _slack_doc_ext(filename)
    digest = hashlib.sha256(key_url.encode("utf-8")).hexdigest()[:16]
    target = config.PDFS_DIR / f"{digest}{ext}"
    if target.exists() and target.stat().st_size > 0:
        return (str(target), target.stat().st_size)
    body, content_type = _fetch(download_url)
    if body is None:
        return None
    if ext == ".pdf":
        if not _is_pdf_payload(content_type, body):
            return None
    elif _looks_like_html(content_type, body):
        return None                       # expired-token login page, not the file
    return _write_if_within_limit(target, body)


def copy_to_upload_queue(local_pdf_path: str) -> None:
    """
    Copy a pdf_saved PDF into pdfs_to_upload/ for Zotero desktop drag-in.
    Called by enrich_paper only when metadata_source ends up as pdf_saved —
    translator+PDF papers are excluded because their PDF is attached
    programmatically during upload instead.
    """
    src = Path(local_pdf_path)
    queue_dir = config.PROJECT_ROOT / "pdfs_to_upload"
    queue_dir.mkdir(parents=True, exist_ok=True)
    dest = queue_dir / src.name
    if not dest.exists():
        shutil.copy2(src, dest)


# ── HTTP fetch ────────────────────────────────────────────────────────────────

def _fetch(url: str) -> tuple[Optional[bytes], str]:
    """
    GET a URL, returning (body, content_type). Body is None on any failure.

    Delegates to http_fetch.fetch (browser impersonation via curl_cffi when
    installed), so PDF downloads get past the same fingerprint-gated publisher
    CDNs that block the metadata fetch — for open-access files. (Passing the
    bot gate is not passing a paywall: a subscription-only PDF still won't
    download, which is fine; those go to the Zotero-desktop drag-in path.)
    Reads up to PDF_FETCH_BYTES so a runaway server can't fill memory, and
    retries once on a transient failure. (http_fetch also returns an HTTP
    status, which PDF downloading doesn't need — dropped here.)
    """
    body, content_type, _status = http_fetch.fetch(url, max_bytes=config.PDF_FETCH_BYTES)
    return body, content_type


# ── Type detection ────────────────────────────────────────────────────────────

def _is_pdf_payload(content_type: str, body: bytes) -> bool:
    """
    A response is a PDF iff the body starts with %PDF. Content-Type alone is
    not trusted: some publishers serve paywall HTML with Content-Type=
    application/pdf, and some open-access mirrors serve PDFs as
    application/octet-stream. Magic bytes are the source of truth.
    """
    return body.startswith(b"%PDF")


def _looks_like_html(content_type: str, body: bytes) -> bool:
    if "text/html" in content_type or "application/xhtml" in content_type:
        return True
    head = body[:200].lower()
    return b"<html" in head or b"<!doctype html" in head


def _looks_like_pdf_file(path) -> bool:
    """Cheap on-disk check used for the idempotency early-return."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


# ── citation_pdf_url meta-tag extraction ──────────────────────────────────────

# Google-Scholar / Zotero standard. Most major academic publishers include
# either citation_pdf_url or (less commonly) og:pdf_url in the page head.
# We accept both attribute orderings (name= before content= or vice versa)
# and both meta names. Case-insensitive on attribute names and the meta-name
# value; the URL itself is preserved as-is.
_CITATION_PDF_RE = re.compile(
    rb'<meta[^>]+(?:name|property)\s*=\s*["\']'
    rb'(?:citation_pdf_url|og:pdf_url)["\']'
    rb'[^>]*content\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
# Same shape, with the attribute order flipped (content= first).
_CITATION_PDF_RE_FLIPPED = re.compile(
    rb'<meta[^>]+content\s*=\s*["\']([^"\']+)["\']'
    rb'[^>]*(?:name|property)\s*=\s*["\']'
    rb'(?:citation_pdf_url|og:pdf_url)["\']',
    re.IGNORECASE,
)


def _extract_citation_pdf_url(html: bytes, base: str) -> Optional[str]:
    """
    Pull the citation_pdf_url from an HTML response. Resolves relative URLs
    against `base` so '/content/foo.pdf'-style hrefs become absolute.
    Returns None if no match.
    """
    for pattern in (_CITATION_PDF_RE, _CITATION_PDF_RE_FLIPPED):
        m = pattern.search(html)
        if m:
            href = m.group(1).decode("utf-8", errors="replace").strip()
            if href:
                return urljoin(base, href)
    return None


# ── File layout ───────────────────────────────────────────────────────────────

def _target_path_for(url: str):
    """Hash-based filename in the configured PDFs directory."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return config.PDFS_DIR / f"{digest}.pdf"


def _write_if_within_limit(target, body: bytes) -> Optional[tuple[str, int]]:
    """Honor the size cap, write atomically, return (path, size)."""
    if len(body) > config.PDF_MAX_BYTES:
        return None     # too big — skip rather than half-fill the disk
    tmp = target.with_name(target.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return (str(target), len(body))


# ── Manual-upload queue (copies of pdf_saved papers' files) ──────────────────

def prepare_upload_queue(db, output_dir: Path) -> dict:
    """
    Wipe `output_dir` and rebuild it with copies of every PDF for papers
    currently at `metadata_source = "pdf_saved"`. The lab member then drags
    the contents of `output_dir` into Zotero desktop to import.

    Papers at `metadata_source = "translator"` (or crossref_doi / llm) with
    a `local_pdf_path` are deliberately NOT copied here — those will be
    uploaded programmatically via pyzotero with their metadata, and the PDF
    will be attached as part of that upload. Including them here would
    cause duplicates in Zotero.

    The queue is regenerated from scratch on every call, so it can never
    drift out of sync with the database state — running it twice in a row
    produces an identical folder. Use this rather than maintaining a
    long-lived queue with state-management logic.

    Returns a counts dict: {copied, skipped_missing, total_eligible}.
    """
    output_dir = Path(output_dir)

    # Wipe — always rebuild fresh, no incremental state to worry about.
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    counts = {"copied": 0, "skipped_missing": 0, "total_eligible": 0}

    for paper in db.all_papers():
        if paper.get("metadata_source") != "pdf_saved":
            continue
        counts["total_eligible"] += 1

        local_path = paper.get("local_pdf_path")
        if not local_path:
            counts["skipped_missing"] += 1
            continue

        src = Path(local_path)
        if not src.exists() or not src.is_file():
            counts["skipped_missing"] += 1
            continue

        try:
            shutil.copy2(src, output_dir / src.name)
            counts["copied"] += 1
        except OSError:
            counts["skipped_missing"] += 1

    return counts
