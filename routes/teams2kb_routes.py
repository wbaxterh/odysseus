"""
teams2kb routes (fork feature — see FORK.md).

Lets the enhanced Teams Chat Exporter extension push a chat export straight
into the knowledge base: the extension POSTs its raw export JSON plus taxonomy
metadata; this route converts it through the teams2kb CLI (the contract-tested
converter), chunks it (per-message + conversation windows), and embeds it via
the app's VectorRAG singleton.

Auth: rides the normal middleware — callers present a standard Odysseus API
token (Authorization: Bearer ody_…) or the internal-tool header. The extra
X-Teams2KB-Ingest header is a CSRF hard-stop: it forces a CORS preflight for
any web-origin caller, which this API never approves; the extension bypasses
CORS via its host permission.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/teams2kb", tags=["teams2kb"])

T2K_DIR = Path(os.environ.get("TEAMS2KB_DIR", "/Users/weshuber/Documents/HuberSoftware/teams2kb"))
INGESTED_DIR = T2K_DIR / "ingested"  # stable paths so re-ingest dedupes and --remove works
DEFAULT_CATEGORIES = ["work", "hobby", "passion", "finances", "relationships", "wellness"]
DEFAULT_OWNER = "wes"


def _owner_from(request: Request) -> str:
    user = getattr(request.state, "current_user", None)
    if user and user != "internal-tool":
        return user
    return DEFAULT_OWNER


def _require_ingest_header(request: Request) -> None:
    if not request.headers.get("X-Teams2KB-Ingest"):
        raise HTTPException(400, "Missing X-Teams2KB-Ingest header")


def _rag():
    from src.rag_singleton import get_rag_manager
    rag = get_rag_manager()
    if rag is None or not rag.healthy:
        raise HTTPException(503, "Knowledge base unavailable (is ChromaDB running?)")
    return rag


def _node_bin() -> str:
    for candidate in (
        os.environ.get("TEAMS2KB_NODE"),
        shutil.which("node"),
        "/Users/weshuber/.nvm/versions/node/v23.10.0/bin/node",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    raise HTTPException(500, "node not found — set TEAMS2KB_NODE")


def _t2k_module():
    integrations = str(T2K_DIR / "integrations" / "odysseus")
    if integrations not in sys.path:
        sys.path.insert(0, integrations)
    import ingest_teams_chat  # noqa: PLC0415
    return ingest_teams_chat


@router.get("/meta")
def teams2kb_meta(request: Request):
    """Distinct taxonomy values for the extension's dropdowns."""
    rag = _rag()
    owner = _owner_from(request)
    categories, orgs, projects = set(), set(), set()
    try:
        res = rag.collection.get(where={"owner": owner}, include=["metadatas"])
        for md in res.get("metadatas") or []:
            if md.get("category"):
                categories.add(str(md["category"]))
            if md.get("project"):
                projects.add(str(md["project"]))
            for org in str(md.get("org") or "").split(","):
                if org.strip():
                    orgs.add(org.strip())
    except Exception as e:
        logger.warning("teams2kb meta scan failed: %s", e)
    return {
        "ok": True,
        "owner": owner,
        "categories": sorted(categories | set(DEFAULT_CATEGORIES)),
        "orgs": sorted(orgs),
        "projects": sorted(projects),
    }


@router.post("/ingest")
def teams2kb_ingest(request: Request, body: dict):
    """Raw extension export JSON + taxonomy tags → converted → embedded."""
    _require_ingest_header(request)
    export = body.get("export")
    if not isinstance(export, dict) or "meta" not in export or "messages" not in export:
        raise HTTPException(400, "Body must include export: {meta, messages}")

    rag = _rag()
    owner = _owner_from(request)
    t2k = _t2k_module()

    title = (export.get("meta") or {}).get("title") or "teams-chat"
    conv = (export.get("meta") or {}).get("conversationId") or ""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "teams-chat"
    suffix = re.sub(r"[^a-zA-Z0-9]", "", conv)[-10:] or "chat"
    INGESTED_DIR.mkdir(parents=True, exist_ok=True)
    export_path = INGESTED_DIR / f"{slug}-{suffix}.export.json"
    export_path.write_text(json.dumps(export), encoding="utf-8")

    # Convert through the golden-tested teams2kb CLI — one converter, one contract.
    result = subprocess.run(
        [_node_bin(), str(T2K_DIR / "dist" / "cli.js"), str(export_path),
         "-o", str(INGESTED_DIR), "--jsonl"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        logger.error("teams2kb convert failed: %s", result.stderr.strip()[:500])
        raise HTTPException(422, f"Conversion failed: {result.stderr.strip()[:200]}")
    jsonl_candidates = sorted(
        INGESTED_DIR.glob("teams_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not jsonl_candidates:
        raise HTTPException(500, "Converter produced no JSONL output")
    jsonl_path = jsonl_candidates[0]

    # Chunk exactly like the CLI ingester: messages + windows, link hosts, dedupe.
    tags = t2k.build_tags(SimpleNamespace(
        owner=owner,
        category=(body.get("category") or "").strip() or None,
        org=(body.get("org") or "").strip() or None,
        project=(body.get("project") or "").strip() or None,
        tag=None,
    ))
    meta, messages = t2k.parse_jsonl(jsonl_path)
    by_id = {m["id"]: m for m in messages}
    docs, seen = [], set()
    # Same selection as the CLI ingester: message chunks keep attachment-only
    # messages; windows are built from text-bearing messages only.
    kept = [
        m for m in messages
        if not m.get("system") and not m.get("deleted")
        and (m.get("text") or m.get("attachments"))
    ]
    for msg in kept:
        text = t2k.chunk_text(meta, msg, by_id)
        if text not in seen:
            seen.add(text)
            docs.append((text, t2k.chunk_metadata(meta, msg, jsonl_path, tags)))
    n_msgs = len(docs)
    window_src = [m for m in kept if m.get("text")]
    for text, md in t2k.window_chunks(meta, window_src, jsonl_path, tags, 5, 3):
        if text not in seen:
            seen.add(text)
            docs.append((text, md))

    batch = rag.add_documents_batch(docs)
    if not batch.get("success"):
        raise HTTPException(502, f"Embedding failed: {batch.get('message', 'unknown')}")
    added = batch.get("added_count", 0)
    stats = rag.get_stats() or {}
    logger.info("teams2kb ingest: %s — %d queued, %d added (owner=%s)",
                meta.get("chat_title"), len(docs), added, owner)
    return {
        "ok": True,
        "chat_title": meta.get("chat_title"),
        "owner": owner,
        "queued": len(docs),
        "windows": len(docs) - n_msgs,
        "added": added,
        "deduped": len(docs) - added,
        "kb_total": stats.get("document_count"),
        "jsonl_path": str(jsonl_path),
    }
