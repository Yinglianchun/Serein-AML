import gzip
import json

import pytest

from serein.core import Store
from serein.core.search import Search, build_index, tokens
from serein.core.store import digest


@pytest.fixture
def search_data(tmp_path):
    database, index = tmp_path / "canonical.db", tmp_path / "search.db"
    with Store(database) as store:
        store.create("event_a", "event", "雨天归航", "我们在雨天看世界之窗")
        store.create("scene_a", "scene", "世界之窗", "雨天的世界之窗")
        store.create("event_old", "event", "旧雨天", "世界之窗旧经历", lifecycle="archived")
        store.create("event_manual", "event", "私下保存", "世界之窗", manual_surface=False)
        store.create("narrative_a", "narrative", "世界之窗", "长篇叙事")
        source = store.add_source("message/1", "原文证据")
        store.bind("event_a", source)
    build_index(database, index)
    return database, index


def ids(result):
    return {item["id"] for item in result["items"]}


def test_mixed_chinese_english_token_boundaries():
    assert tokens("Codex归航 SQLite数据库") == ["codex", "归航", "sqlite", "数据", "据库"]


def test_chinese_query_returns_current_body_and_evidence(search_data):
    with Search(*search_data) as search:
        result = search.search("世界之窗", with_evidence=True)
        assert ids(result) == {"event_a", "scene_a"}
        event = next(item for item in result["items"] if item["id"] == "event_a")
        assert event["object"]["evidence"][0]["content"] == "原文证据"
        assert search.search("世界之窗 火星")["status"] == "no_match"
        assert search.search(" ")["status"] == "empty_or_unsupported_query"
        assert search.search("雨")["status"] == "empty_or_unsupported_query"
        assert search.search('" OR *')["status"] == "no_match"
        assert not result["injected"]
        assert search.reader.store.conn.total_changes == search.conn.total_changes == 0


def test_explicit_lookup_is_separate_from_automatic_surfacing(search_data):
    with Search(*search_data) as search:
        assert ids(search.search("世界之窗", mode="lookup", kind="event")) == {"event_a", "event_old", "event_manual"}
        assert ids(search.search("世界之窗", mode="lookup", kind="narrative")) == {"narrative_a"}
        with pytest.raises(ValueError, match="Narrative"):
            search.search("世界之窗", kind="narrative")


def test_current_evidence_coverage_replaces_old_index_policy(search_data):
    database, index = search_data
    with Store(database) as store:
        source = store.conn.execute("SELECT source_id FROM evidence_bindings WHERE document_id='event_a'").fetchone()[0]
        store.bind("scene_a", source)
    with Search(database, index) as search:
        result = search.search("世界之窗")
        assert ids(result) == {"scene_a"}
        assert result["suppressed"]["covered_by_scene"] == 1
    with Store(database) as store:
        store.set_lifecycle("scene_a", "archived")
    with Search(database, index) as search:
        assert ids(search.search("世界之窗")) == {"event_a"}


def test_changed_deleted_and_replaced_candidates_never_return_stale_text(search_data):
    database, index = search_data
    with Store(database) as store:
        store.revise("scene_a", expected_revision=1, title="新标题", body_md="完全不同的正文")
        store.set_lifecycle("event_manual", "deleted")
        store.conn.execute("INSERT INTO event_replacements VALUES ('event_a','event_other','test','{}')")
    with Search(database, index) as search:
        result = search.search("世界之窗")
        assert result["items"] == []
        assert result["suppressed"]["stale_content_rebuild_required"] == 1
        assert result["suppressed"]["deleted"] == 1
        assert result["suppressed"]["replaced_by_event"] == 1
        assert "私下保存" not in json.dumps(result, ensure_ascii=False)
    replacement = index.with_name("rebuilt.db")
    build_index(database, replacement)
    with Search(database, replacement) as search:
        assert ids(search.search("完全不同")) == {"scene_a"}
    with pytest.raises(FileExistsError):
        build_index(database, index)
    with pytest.raises(FileExistsError):
        build_index(database, database)


def test_legacy_vectors_require_original_input_and_explicit_query_profile(tmp_path):
    database, index, cache_path = tmp_path / "canonical.db", tmp_path / "index.db", tmp_path / "cache.json.gz"
    profile = {"model": "test-model", "provider_host": "test.invalid", "document_instruction": "",
               "query_instruction": "retrieve", "max_chars": 6000}
    rows = []
    with Store(database) as store:
        for key, body, vector in [("event_a", "归航", [1, 0]), ("event_b", "其他", [0, 1]),
                                  ("event_changed", "旧正文", [1, 0])]:
            meta = {"item_id": key, "item_type": "event", "fingerprint": key, "body": body}
            store.create(key, "event", key, "新正文" if key == "event_changed" else body, metadata=meta)
            stamp = {"schema": 1, **meta, "profile": {key: profile[key] for key in
                                                    ("model", "document_instruction", "max_chars")}}
            rows.append({"item_id": key, "item_type": "event", "model": profile["model"], "dimension": 2,
                         "source_hash": digest(json.dumps(stamp, ensure_ascii=False, sort_keys=True)),
                         "embedding": json.dumps(vector)})
    with gzip.open(cache_path, "wt", encoding="utf-8") as file:
        json.dump({"profile": profile, "event_vectors": rows}, file)
    report = build_index(database, index, event_cache=cache_path)
    assert report["counts"]["event_vectors"] == 2
    assert report["counts"]["vectors_rejected"] == 1
    query = {"query": "回家", "profile": profile, "embedding": [1, 0]}
    with Search(database, index) as search:
        assert ids(search.search("回家", query_embedding=query, min_cosine=0.9)) == {"event_a"}
        with pytest.raises(ValueError, match="min_cosine"):
            search.search("回家", query_embedding=query)
        for bad in [{**query, "query": "different"},
                    {**query, "profile": {**profile, "provider_host": "another.invalid"}},
                    {**query, "embedding": [1]}, {**query, "embedding": [0, 0]},
                    {**query, "embedding": [float("nan"), 0]}]:
            with pytest.raises(ValueError):
                search.search("回家", query_embedding=bad, min_cosine=0.9)
    with Store(database) as store:
        store.set_manual_surface("event_a", False)
    with Search(database, index) as search:
        assert search.search("回家", query_embedding=query, min_cosine=0.9)["items"] == []
