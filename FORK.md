# Fork notes — wbaxterh/odysseus

This checkout runs a personal fork of
[pewdiepie-archdaemon/odysseus](https://github.com/pewdiepie-archdaemon/odysseus)
with custom features that are **not** intended for upstream. This file tracks
what's custom and how to keep custom work and upstream contributions separate.

## Branch model

| Branch | Purpose |
|---|---|
| `wes/custom` | **The running version** (this checkout). Upstream base + custom features. Never PR'd. |
| `main` | Clean mirror of `upstream/main`. Never commit here. |
| `fix/*`, `feature/*`, `docs/*` | Contribution branches, cut from `upstream/main`, pushed to `origin`, PR'd upstream. Developed in the `../odysseus-contrib` worktree so this running checkout is undisturbed. |

## Custom features on `wes/custom`

- **teams2kb ingest endpoint** (`routes/teams2kb_routes.py`, registered in
  `app.py`): `GET /api/teams2kb/meta` (taxonomy values for the extension's
  dropdowns) and `POST /api/teams2kb/ingest` (raw Teams Chat Exporter JSON →
  teams2kb CLI conversion → message+window chunks → VectorRAG). Auth: normal
  middleware (Bearer `ody_` API token or internal-tool header) plus an
  `X-Teams2KB-Ingest` CSRF-stop header. Converted JSONL lands in
  `teams2kb/ingested/` (gitignored) so re-ingest dedupes and removal works.
  Client: the enhanced extension fork at
  `~/Documents/HuberSoftware/teams2kb-extension` (branch `t2k/ingest-ui`),
  pushed to the private repo
  [wbaxterh/teams2kb-extension](https://github.com/wbaxterh/teams2kb-extension)
  (`upstream` remote = gediz/teams-web-chat-exporter).

- **Reasoned multi-source RAG** (`src/rag_reasoned.py` + the RAG block in
  `src/chat_processor.py`): answer-time retrieval gathers conversation
  evidence (Teams chats ingested by
  [teams2kb](https://github.com/wbaxterh/teams2kb)) and document evidence as
  separate families, annotates provenance + timestamps, and injects a
  reasoning guide (recency, source kind, conflicts, attribution).
  Self-contained: works with or without VectorRAG metadata-filter support
  (falls back to Python-side filtering), so it survives rebases regardless of
  what lands upstream.

- **`search_knowledge` agent tool** (impl `src/tools/search.py`, schema
  `src/tool_schemas.py`, dispatch `src/tool_execution.py`, aliases
  `src/tool_parsing.py`, index `src/tool_index.py`, one-liner + domain map
  `src/agent_loop.py`, admin catalog `static/js/admin.js`): agent-mode
  front-end for the reasoned-RAG retrieval, owner-scoped like
  `search_chats`, so Agent mode can query ingested Teams chats/documents on
  demand instead of relying on the chat-mode preface injection.

- **teams2kb owner attribution** (`routes/teams2kb_routes.py`): `_owner_from`
  resolves bearer `ody_` callers via `src.auth_helpers.effective_user` so
  extension ingests land under the token's human owner, not the `api`
  pseudo-user silo.

- **Chat-mode RAG default-on + visible toggle** (`static/app.js`): RAG init
  defaults ON unless the user explicitly toggled it off (`rag_user_set`
  marker; the old init persisted `rag:false` on every load), and
  `rag-toggle-btn` was removed from `UI_VIS_DEFAULT_OFF` so the RAG item
  shows in the composer "+" menu. Matches the backend default (`use_rag`
  omitted → RAG on).

- **API Tokens settings card restore** (`static/index.html`): the settings
  overhaul commit 4f7061f dropped the API Tokens markup from the System
  panel while `admin.js` kept `loadTokens`/`initTokenForm` wired, leaving
  token management invisible. Markup restored (likely upstreamable).

## Workflows

**Contribute upstream** (in the sibling worktree, running checkout untouched):

```bash
cd ../odysseus-contrib
git fetch upstream
git checkout -b fix/my-fix upstream/main
# ...work, commit...
git push -u origin fix/my-fix   # then open the PR against upstream
```

(The contrib worktree shares this repo's git history; reuse this checkout's
venv via `~/odysseus/venv/bin/python` when running tests there.)

**Pull upstream into the running version:**

```bash
git fetch upstream
git checkout main && git merge --ff-only upstream/main
git checkout wes/custom && git rebase main   # replay custom features on top
```

The custom surface is deliberately tiny (one new module + one block in
`chat_processor.py`) to keep those rebases near-conflict-free.

**Add a new custom feature:** commit it on `wes/custom` and list it above. If
part of it is genuinely upstreamable, extract that part into a contribution
branch from `upstream/main` — don't PR from `wes/custom`.
