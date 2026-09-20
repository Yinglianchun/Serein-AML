import hashlib
import json
from typing import Any
from .narrative_writer_sources import conversation_source_messages
from ...recall.germany.utils import strip_wikilinks

def diary_comments_sha256(comments: Any) -> str:
    """Hash the stable, readable comment snapshot returned with a Diary."""

    normalized = [
        {
            "id": int(comment.get("id") or 0),
            "author": str(comment.get("author") or ""),
            "created_at": str(comment.get("created_at") or ""),
            "content": str(comment.get("content") or ""),
        }
        for comment in comments or []
        if isinstance(comment, dict)
    ]
    normalized.sort(
        key=lambda comment: (
            comment["id"],
            comment["author"],
            comment["created_at"],
            comment["content"],
        )
    )
    stable_json = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(stable_json.encode("utf-8")).hexdigest()

def materialize_sources(narrative: dict, fact_event_store, scenes_store, diary_store, narrative_upload_store) -> dict:
    """Read the exact frozen direct membership used by one Writer preview."""
    events: list[dict] = []
    scenes: list[dict] = []
    diaries: list[dict] = []
    darkrooms: list[dict] = []
    uploads: list[dict] = []
    errors: list[dict] = []

    for event_id in narrative.get("linked_event_ids") or []:
        try:
            event = fact_event_store.read(str(event_id), include_sources=True)
        except ValueError:
            event = None
        if not event:
            errors.append({"source_type": "event", "source_id": str(event_id), "reason": "not_found"})
            continue
        if str(event.get("item_type") or "") != "event" or str(event.get("status") or "") != "active":
            errors.append({"source_type": "event", "source_id": str(event_id), "reason": "not_active"})
            continue
        conversation = conversation_source_messages(event.get("source_refs") or [])
        if conversation["status"] == "invalid":
            errors.append({
                "source_type": "event",
                "source_id": str(event_id),
                "message_id": conversation.get("message_id"),
                "reason": "source_snapshot_mismatch",
            })
            continue
        event_material = {
            "source_type": "event",
            "event_id": str(event_id),
            "title": str(event.get("title") or ""),
            "date": str(event.get("local_date") or ""),
            "fingerprint": str(event.get("fingerprint") or ""),
        }
        if conversation["status"] == "ok":
            event_material.update({
                "source_mode": "conversation",
                "source_messages": conversation["messages"],
            })
        else:
            summary = str(event.get("body") or "").strip()
            if not summary:
                errors.append({"source_type": "event", "source_id": str(event_id), "reason": "empty_content"})
                continue
            event_material.update({
                "source_mode": "material",
                "summary": summary,
                "content_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            })
        events.append(event_material)

    for scene_id in narrative.get("linked_scene_ids") or []:
        scene = scenes_store.read(str(scene_id))
        if not scene or scene.get('status')=='not_found':
            errors.append({"source_type": "scene", "source_id": str(scene_id), "reason": "not_found"})
            continue
        meta = scene.get("metadata", {}) if isinstance(scene.get("metadata"), dict) else {}
        if (
            scene.get('object_kind') != 'scene'
            or meta.get("active") is False
            or meta.get("deprecated")
            or meta.get("resolved")
            or meta.get("digested")
        ):
            errors.append({"source_type": "scene", "source_id": str(scene_id), "reason": "not_active"})
            continue
        conversation = conversation_source_messages(scenes_store.evidence(str(scene_id)).get('evidence_refs') or [])
        if conversation["status"] == "invalid":
            errors.append({
                "source_type": "scene",
                "source_id": str(scene_id),
                "message_id": conversation.get("message_id"),
                "reason": "source_snapshot_mismatch",
            })
            continue
        scene_material = {
            "source_type": "scene",
            "scene_id": str(scene_id),
            "title": str(meta.get("name") or scene_id),
            "date": str(meta.get("date") or meta.get("event_date") or meta.get("created") or ""),
            "status": "active",
        }
        if conversation["status"] == "ok":
            scene_material.update({
                "source_mode": "conversation",
                "source_messages": conversation["messages"],
                "content_sha256": hashlib.sha256(
                    "".join(message["content_sha256"] for message in conversation["messages"]).encode("utf-8")
                ).hexdigest(),
            })
        else:
            content = strip_wikilinks(str(scene.get("content") or "")).strip()
            if not content:
                errors.append({"source_type": "scene", "source_id": str(scene_id), "reason": "empty_content"})
                continue
            scene_material.update({
                "source_mode": "material",
                "content": content,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            })
        scenes.append(scene_material)

    def read_diary_source(source_id: int, source_type: str) -> dict | None:
        result = diary_store.read(diary_id=int(source_id), limit=1, include_archived=True)
        if result.get("count") != 1 or int(result.get("id") or 0) != int(source_id):
            errors.append({"source_type": source_type, "source_id": int(source_id), "reason": "not_found"})
            return None
        expected_entry_type = "darkroom" if source_type == "darkroom" else "diary"
        if str(result.get("entry_type") or "") != expected_entry_type:
            errors.append({"source_type": source_type, "source_id": int(source_id), "reason": "wrong_source_type"})
            return None
        if str(result.get("visibility") or "") != "active":
            errors.append({"source_type": source_type, "source_id": int(source_id), "reason": "not_active"})
            return None
        if bool(result.get("locked")) or not bool(result.get("body_available", True)):
            errors.append({"source_type": source_type, "source_id": int(source_id), "reason": "locked_or_unavailable"})
            return None
        content = str(result.get("content") or "").strip()
        if not content:
            errors.append({"source_type": source_type, "source_id": int(source_id), "reason": "empty_content"})
            return None
        comments = list(result.get("comments") or [])
        return {
            "source_type": source_type,
            "source_mode": "direct",
            f"{source_type}_id": int(source_id),
            "title": str(result.get("title") or f"{source_type} {source_id}"),
            "date": str(result.get("date") or ""),
            "revision": int(result.get("revision") or 1),
            "content": content,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "comments_sha256": diary_comments_sha256(comments),
            "comments": [
                {
                    "author": str(comment.get("author") or ""),
                    "created_at": str(comment.get("created_at") or ""),
                    "content": str(comment.get("content") or ""),
                }
                for comment in comments
                if str(comment.get("content") or "").strip()
            ],
        }

    for diary_id in narrative.get("linked_diary_ids") or []:
        item = read_diary_source(int(diary_id), "diary")
        if item:
            diaries.append(item)
    for darkroom_id in narrative.get("linked_darkroom_ids") or []:
        item = read_diary_source(int(darkroom_id), "darkroom")
        if item:
            darkrooms.append(item)
    for upload_id in narrative.get("linked_upload_ids") or []:
        item = narrative_upload_store.read(str(upload_id), include_text=True)
        if item.get("status") != "ok":
            errors.append({
                "source_type": "upload",
                "source_id": str(upload_id),
                "reason": str(item.get("reason") or item.get("status") or "not_found"),
            })
            continue
        uploads.append({
            "source_type": "upload",
            "source_mode": "direct",
            "upload_id": str(item.get("upload_id") or ""),
            "title": str(item.get("filename") or upload_id),
            "filename": str(item.get("filename") or ""),
            "content_type": str(item.get("content_type") or ""),
            "size": int(item.get("size") or 0),
            "sha256": str(item.get("sha256") or ""),
            "extraction_status": str(item.get("extraction_status") or ""),
            "content": str(item.get("extracted_text") or ""),
        })

    if errors:
        return {"status": "invalid", "reason": "bound_material_unavailable", "errors": errors}
    return {
        "status": "ok",
        "narrative": {
            "narrative_id": str(narrative.get("narrative_id") or ""),
            "title": str(narrative.get("title") or ""),
            "arc_key": str(narrative.get("arc_key") or ""),
            "time_start": str(narrative.get("time_start") or ""),
            "time_end": str(narrative.get("time_end") or ""),
        },
        "events": events,
        "scenes": scenes,
        "diaries": diaries,
        "darkrooms": darkrooms,
        "uploads": uploads,
        "material_counts": {
            "events": len(events),
            "scenes": len(scenes),
            "diaries": len(diaries),
            "darkrooms": len(darkrooms),
            "uploads": len(uploads),
        },
    }
