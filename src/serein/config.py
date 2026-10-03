"""Deployment settings live outside the shared core; no implicit local lookup."""

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Settings:
    database: Path
    index: Path | None = None
    extensions: dict = field(default_factory=dict, repr=False)
    writable: bool = False
    source: dict = field(default_factory=dict, repr=False)
    embedding: dict = field(default_factory=dict, repr=False)
    recall: dict = field(default_factory=dict)
    reranker: dict = field(default_factory=dict, repr=False)
    background: dict = field(default_factory=dict, repr=False)
    mcp_tools: list[str] | None = None


def load_settings(path):
    path = Path(path).resolve()
    with path.open("rb") as file:
        raw = tomllib.load(file)
    unknown = raw.keys() - {"storage", "extensions", "runtime", "source", "embedding", "recall", "reranker", "background", "mcp"}
    if unknown:
        raise ValueError(f"Unknown configuration sections: {', '.join(sorted(unknown))}")
    storage = raw.get("storage", {})
    if "database" not in storage or storage.keys() - {"database", "index"}:
        raise ValueError("storage requires database and optionally index")

    def location(value):
        target = Path(value)
        return (path.parent / target).resolve() if not target.is_absolute() else target.resolve()

    extensions = raw.get("extensions", {})
    for name, options in extensions.items():
        if not isinstance(options, dict) or type(options.get("enabled", False)) is not bool:
            raise ValueError(f"Extension {name} requires a table with a boolean enabled setting")
    runtime = raw.get("runtime", {})
    if runtime.keys() - {"writable"} or type(runtime.get("writable", False)) is not bool:
        raise ValueError("runtime supports a boolean writable setting")
    source = raw.get("source", {})
    if source:
        if source.keys() - {"database", "session_ids", "system"} or not all(k in source for k in ("database", "session_ids", "system")):
            raise ValueError("source requires database, explicit session_ids, and a stable system name")
        if not source["system"] or not isinstance(source["session_ids"], list):
            raise ValueError("Source system and explicit session_ids are required")
        source = {**source, "database": location(source["database"])}
    embedding = raw.get("embedding", {})
    if embedding and (set(embedding) != {"endpoint", "api_key_env"} or not all(embedding.values())):
        raise ValueError("embedding requires endpoint and api_key_env")
    from .recall.policy import RecallPolicy
    recall = raw.get("recall", {})
    RecallPolicy.from_config(recall)
    if embedding and not recall.get('routing_file'):
        raise ValueError('Semantic recall requires recall.routing_file; prepare generic routes before enabling the provider')
    for field in ("routing_file", "germany_policy_file"):
        if recall.get(field):
            recall = {**recall, field: str(location(recall[field]))}
    reranker = raw.get("reranker", {})
    if reranker and (set(reranker) != {"endpoint", "model", "api_key_env"} or not all(isinstance(value, str) and value for value in reranker.values())):
        raise ValueError("reranker requires endpoint, model, and api_key_env")
    background = raw.get('background', {})
    if background and (set(background) != {'germany_config_file'} or not isinstance(background['germany_config_file'], str)):
        raise ValueError('background requires germany_config_file')
    if background:
        background = {'germany_config_file': str(location(background['germany_config_file']))}
    mcp = raw.get('mcp',{})
    if mcp.keys()-{'tools'} or ('tools' in mcp and
        (not isinstance(mcp['tools'],list) or any(not isinstance(t,str) for t in mcp['tools']))):
        raise ValueError('mcp.tools must be an explicit list of tool names')
    return Settings(location(storage["database"]), location(storage["index"]) if storage.get("index") else None,
                    extensions, runtime.get("writable", False), source, embedding, recall, reranker, background, mcp.get('tools'))
