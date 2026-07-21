"""
rag_reasoned.py — multi-source evidence retrieval for RAG answers.

Instead of one flat search, gather evidence from the KB's source families
separately — conversations (Teams chats ingested by teams2kb) and documents
(files, spreadsheets, charts/vision descriptions) — annotate every item with
its provenance and timestamp, and emit a structured evidence block that tells
the model to reason across sources (recency, source kind, conflicts,
attribution) before it answers.

Why scoped retrieval: in a mixed KB, document chunks share vocabulary with
conversations and bury them — measured on a real chat corpus: 7/10
attribution hit@5 flat vs 10/10 scoped (teams2kb eval, 2026-07-21).
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONVERSATION_TYPES = {"teams-chat"}


def _kind(meta: Dict[str, Any]) -> str:
    return "conversation" if meta.get("type") in CONVERSATION_TYPES else "document"


def _label(meta: Dict[str, Any]) -> str:
    parts = []
    if _kind(meta) == "conversation":
        if meta.get("chat_title"):
            parts.append(str(meta["chat_title"]))
        if meta.get("date"):
            parts.append(str(meta["date"]))
        if meta.get("author") and meta.get("granularity") != "window":
            parts.append(str(meta["author"]))
        if meta.get("granularity") == "window":
            parts.append("conversation excerpt")
    else:
        fn = meta.get("filename") or (meta.get("source") or "unknown").split("/")[-1]
        low = fn.lower()
        for ext in (".docx.md", ".pptx.md", ".xlsx.md", ".doc.md", ".ppt.md", ".xls.md"):
            if low.endswith(ext):
                fn = fn[:-3]
                break
        parts.append(fn)
    if meta.get("project"):
        parts.append(f"project: {meta['project']}")
    if meta.get("org"):
        parts.append(f"org: {meta['org']}")
    return " · ".join(parts)


def _search(rag_manager, query, k, owner, filters):
    """Scoped search that works on any Odysseus base: uses the `filters`
    kwarg where VectorRAG supports metadata filtering, otherwise falls back
    to a wider owner-only search filtered in Python. Keeps this fork feature
    self-contained — no dependency on unmerged search-filter changes."""
    try:
        return rag_manager.search(query, k=k, owner=owner, filters=filters or None)
    except TypeError:
        results = rag_manager.search(query, k=max(k * 4, 20), owner=owner)
        if filters:
            results = [
                r for r in results
                if all((r.get("metadata") or {}).get(fk) == fv for fk, fv in filters.items())
            ]
        return results[:k]


def gather_evidence(
    rag_manager,
    query: str,
    k: int = 5,
    owner: Optional[str] = None,
    filters: Optional[Dict[str, Any]] = None,
    min_similarity: float = 0.0,
) -> List[Dict[str, Any]]:
    """Retrieve top evidence per source family (k conversations + k documents),
    deduped by id, threshold-filtered, each annotated with kind/label/date."""
    base = dict(filters or {})
    base.pop("type", None)  # family scoping owns the type axis

    convo = _search(rag_manager, query, k, owner, {**base, "type": "teams-chat"})
    mixed = _search(rag_manager, query, k * 2, owner, base)
    docs = [r for r in mixed if _kind(r.get("metadata") or {}) == "document"][:k]

    seen, items = set(), []
    for r in convo + docs:
        if r.get("id") in seen:
            continue
        seen.add(r.get("id"))
        if r.get("similarity", 0) < min_similarity:
            continue
        meta = r.get("metadata") or {}
        items.append({
            "kind": _kind(meta),
            "label": _label(meta),
            "date": meta.get("date") or "",
            "timestamp": meta.get("timestamp") or "",
            "document": r.get("document") or "",
            "similarity": r.get("similarity", 0),
            "metadata": meta,
        })
    # Newest-first within the block so recency is visually obvious to the model.
    items.sort(key=lambda i: (i["kind"], i["timestamp"] or i["date"]), reverse=True)
    return items


REASONING_GUIDE = (
    "Before answering, reason across this evidence:\n"
    "1. Weigh timestamps — newer conversation messages supersede older ones "
    "and may supersede documents written earlier.\n"
    "2. Match the source family to the question: conversations carry "
    "decisions, status, and who-said-what; documents (including chart/table "
    "descriptions) carry specs, data, and reference material.\n"
    "3. If sources conflict, say so explicitly and cite each side with its "
    "date rather than silently picking one.\n"
    "4. Attribute what you use: name the person and date for conversation "
    "evidence, the file for document evidence. If the evidence doesn't "
    "answer the question, say what's missing instead of guessing."
)


def format_evidence(items: List[Dict[str, Any]], max_chars: int = 10000) -> str:
    """The injected context block: evidence grouped by family, then the
    reasoning guide."""
    convos = [i for i in items if i["kind"] == "conversation"]
    docs = [i for i in items if i["kind"] == "document"]

    sections = []
    if convos:
        sections.append("## Conversation evidence (Teams chats)\n\n" + "\n\n---\n\n".join(
            f"[{i['label']}]\n{i['document']}" for i in convos))
    if docs:
        sections.append("## Document evidence (files, data, charts)\n\n" + "\n\n---\n\n".join(
            f"[{i['label']}]\n{i['document']}" for i in docs))

    body = "\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n[Truncated]"
    return f"{body}\n\n{REASONING_GUIDE}"
