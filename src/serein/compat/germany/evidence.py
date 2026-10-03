"""Exact source-ref validation reused from Germany scene_evidence.py."""

from __future__ import annotations

import hashlib

import re

from typing import Any

EVIDENCE_KINDS = frozenset({"primary", "supporting", "adjacent_context"})

HASH_ALGORITHM = "sha256-utf8"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

def content_sha256(content: str) -> str:
    """Hash the supplied string exactly as UTF-8; do not trim or normalize."""

    if not isinstance(content, str):
        raise ValueError("evidence content must be a string")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

def normalize_evidence_ref(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("each evidence ref must be an object")

    source_system = _required_text(raw.get("source_system"), "source_system", 80)
    session_id = _optional_identifier(raw.get("session_id"), 160)
    thread_id = _optional_identifier(raw.get("thread_id"), 200)
    if not session_id and not thread_id:
        raise ValueError("evidence ref requires session_id or thread_id")
    message_id = _required_identifier(raw.get("message_id"), "message_id", 160)
    role = _required_text(raw.get("role"), "role", 40).lower()
    created_at = _required_text(raw.get("created_at"), "created_at", 128)
    binding_method = _required_text(raw.get("binding_method"), "binding_method", 80)
    evidence_kind = str(raw.get("evidence_kind") or "primary").strip().lower()
    if evidence_kind not in EVIDENCE_KINDS:
        raise ValueError(
            "evidence_kind must be one of: " + ", ".join(sorted(EVIDENCE_KINDS))
        )

    has_content = "content" in raw and raw.get("content") is not None
    content = raw.get("content") if has_content else ""
    if has_content and not isinstance(content, str):
        raise ValueError("evidence content must be a string")
    snapshot_ref = str(raw.get("snapshot_ref") or "").strip()[:500]
    supplied_hash = str(raw.get("content_sha256") or "").strip().lower()
    if has_content:
        computed_hash = content_sha256(content)
        if supplied_hash and supplied_hash != computed_hash:
            raise ValueError("content_sha256 does not match exact UTF-8 content")
        supplied_hash = computed_hash
    elif not snapshot_ref:
        raise ValueError("evidence ref requires exact content or snapshot_ref")
    elif not _SHA256_RE.fullmatch(supplied_hash):
        raise ValueError("snapshot-only evidence requires a valid content_sha256")

    return {
        "source_system": source_system,
        "session_id": session_id,
        "thread_id": thread_id,
        "message_id": message_id,
        "role": role,
        "created_at": created_at,
        "content": content,
        "snapshot_ref": snapshot_ref,
        "content_sha256": supplied_hash,
        "hash_algorithm": HASH_ALGORITHM,
        "evidence_kind": evidence_kind,
        "binding_method": binding_method,
    }

def _required_text(value: Any, field: str, limit: int) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} is required")
    return result[:limit]

def _optional_identifier(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]

def _required_identifier(value: Any, field: str, limit: int) -> str:
    result = _optional_identifier(value, limit)
    if not result:
        raise ValueError(f"{field} is required")
    return result
