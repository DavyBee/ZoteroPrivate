# Deploy runbook — Streamlit Community Cloud + Turso

This is the operational runbook for putting the app online. It is **build-ready
but not yet deployed** — work through it only when you're ready to go live.

> ⚠️ **STOP before the final "Deploy" click.** Everything up to that point is
> safe and reversible. Do the final deploy yourself, deliberately.

The app is unchanged between local and cloud: locally it uses a plain SQLite
file and a `.env`; on the cloud it uses Turso (hosted SQLite) and Streamlit's
secrets manager. The only thing that switches the storage backend is the
presence of the two `TURSO_*` secrets — see `database._connect`.

---

## 0. Prerequisites (one-time accounts)

- A **GitHub account** that is NOT the client-visible one for `origin`
  (`DavyBee/ZoteroForEvictionLab`). Community Cloud deploys from a repo, and the
  client must not see this work.
- A **Streamlit Community Cloud** account (sign in at share.streamlit.io with
  the GitHub account above). Free tier allows **one private app** at a time.
- A **Turso** account (turso.tech — no credit card on the free tier).
- The keys you already use locally: `ZOTERO_API_KEY`, `ZOTERO_LIBRARY_ID`,
  `ZOTERO_LIBRARY_TYPE`, optional `ZOTERO_COLLECTION_KEY`, `ANTHROPIC_API_KEY`,
  optional `LLM_MODEL`.

## 1. Create the PRIVATE GitHub repo and push this branch

Create a brand-new **private** repo on the non-client GitHub account, e.g.
`your-private-account/eviction-zotero-web`. Then, from this project:

```bash
git remote add deploy git@github.com:your-private-account/eviction-zotero-web.git
git push deploy port-to-website
```

- Keep pushing `port-to-website` to `deploy` only. **Never** push it to `origin`.
- `DEPLOY.md` and `README.md` are the only Markdown files tracked; the planning
  docs stay off Git entirely (they live in on-device memory).
- The local SQLite file (`*.db`), `.env`, `pdfs/`, and `state/` are gitignored,
  so none of your local data or secrets go up.

## 2. Create the Turso database and migrate data into it

Install the Turso CLI (see turso.tech docs), then:

```bash
turso db create eviction-zotero
turso db show eviction-zotero --url          # → TURSO_DATABASE_URL (libsql://…)
turso db tokens create eviction-zotero       # → TURSO_AUTH_TOKEN
```

Migrate the current library into the (empty) Turso DB by setting the two vars so
`database._connect` routes there, then running the migration:

```bash
TURSO_DATABASE_URL="libsql://eviction-zotero-….turso.io" \
TURSO_AUTH_TOKEN="…" \
python migrate_to_sqlite.py
```

The script refuses to write into a destination that already has papers, and it
verifies the reloaded counts/stats match the JSON before declaring success.
Confirm it prints `Targeting Turso` and `✓ Verified`.

> The local `zotero_database.json` (and any local `.db`) stay untouched as a
> backup. Keep them.

## 3. Configure Community Cloud secrets

In the Streamlit Cloud dashboard for the app, open **Settings → Secrets** and
paste TOML (this is the cloud equivalent of `.env`; `config.load_streamlit_secrets`
bridges it into `os.environ`):

```toml
ZOTERO_API_KEY = "…"
ZOTERO_LIBRARY_ID = "…"
ZOTERO_LIBRARY_TYPE = "group"
ZOTERO_COLLECTION_KEY = ""        # optional
ANTHROPIC_API_KEY = "…"
LLM_MODEL = ""                    # optional — blank uses the built-in default

TURSO_DATABASE_URL = "libsql://eviction-zotero-….turso.io"
TURSO_AUTH_TOKEN = "…"
```

Setting both `TURSO_*` values is what flips storage from a (wiped-on-reboot)
local file to the durable hosted DB. Without them the app would use an ephemeral
local file and lose data on every redeploy — so double-check they're present.

## 4. Make the app private + invite viewers

This app writes to the shared Zotero library, so it must NOT be public.

- In the app's Community Cloud settings, set sharing to **private / invite-only**.
- Add the lab members' emails to the **viewer allowlist**. They sign in with
  Google (or a single-use emailed link) to reach it.

## 5. Deploy (do this last, deliberately)

In Community Cloud: **New app → from existing repo**, pick the private repo, the
`port-to-website` branch, and `app.py` as the entry point. Confirm Python deps
come from `requirements.txt` (it includes `libsql`).

> 🚦 This is the live step. After it, the app is online for the invited viewers.

## 6. Post-deploy checks

- App loads and the status bar shows the migrated counts (≈ what `stats()`
  printed during migration).
- Enrich a single new paper: confirm CrossRef/Citoid/Claude all work over plain
  HTTPS (no Docker).
- Upload one item to Zotero to confirm the write path and secrets.
- Use the **Download a backup copy** button (Settings) to pull a `.db` snapshot;
  keep periodic backups since Turso is the only data home.

## Rollback / safety notes

- The migration never modifies the source JSON; to rebuild, point at a fresh
  Turso DB and re-run step 2.
- A bad redeploy can't lose data: the data lives in Turso, not the app's
  filesystem.
- To take the app offline, delete/undeploy it in Community Cloud; the Turso DB
  (and your data) remain.
