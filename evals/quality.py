"""Run fresh synthetic Add, then compare three retrieval ablations on that DB.

python -m evals.quality --report .local/quality-report.json
Requires an explicitly selected non-mini development model configuration.
No model sees the probe's required/distractor labels. No answer is generated.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import gc
import json
import os
from pathlib import Path
import re
import tempfile
import time

from evals.quality_fixture import fixture


def phrase_present(text, phrase):
    if any("\u4e00" <= character <= "\u9fff" for character in phrase):
        # Public authoring may translate prose and add spaces inside Chinese
        # dates. Keep entire numeral tokens intact (May 2 cannot match May 20).
        tokens = re.findall(r"[A-Za-z0-9_]+|[^\s]", phrase)
        pattern = r"(?<![A-Za-z0-9_])" + r"\s*".join(re.escape(token) for token in tokens) + r"(?![A-Za-z0-9_])"
    else:
        # ASCII names may be embedded directly in Chinese authored sentences.
        pattern = r"(?<![A-Za-z0-9_])" + r"\s+".join(re.escape(part) for part in phrase.split()) + r"(?![A-Za-z0-9_])"
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def supporting_ids(rows, unit):
    # Every phrase must occur in one returned record; names in unrelated records
    # cannot jointly count as a supported relationship. This is still literal
    # coverage, not a semantic judgment of that record's claim.
    return [row["id"] for row in rows if all(
        any(phrase_present(row["content"], phrase) for phrase in alternatives)
        for alternatives in unit)]


def measure(case, rows):
    required = {name: supporting_ids(rows, unit) for name, unit in case["required"].items()}
    distractors = {name: supporting_ids(rows, unit) for name, unit in case["distractors"].items()}
    useful = {key for ids in required.values() for key in ids}
    return {"evidence": required, "missing_units": [name for name, ids in required.items() if not ids],
            "coverage": sum(bool(ids) for ids in required.values()) / len(required),
            "complete_literal_coverage": all(required.values()),
            "distractor_units": {name: ids for name, ids in distractors.items() if ids},
            "unmatched_records": [row["id"] for row in rows if row["id"] not in useful],
            "returned_records": len(rows), "context_characters": sum(len(row["content"]) for row in rows)}


def summarize(results, count):
    return {mode: {"fully_covered_cases": sum(item["complete_literal_coverage"] for item in results if item["mode"] == mode),
                   "cases": count,
                   "mean_coverage": sum(item["coverage"] for item in results if item["mode"] == mode) / count,
                   "context_characters": sum(item["context_characters"] for item in results if item["mode"] == mode)}
            for mode in ("base", "materials", "volumes")}


def rescore(report):
    """Recheck saved synthetic text only; keep the original measures visible."""
    cases = {case["id"]: case for case in fixture()[1]}
    expected = {(key, mode) for key in cases for mode in ("base", "materials", "volumes")}
    pairs = [(item["case"], item["mode"]) for item in report["results"]]
    if len(pairs) != len(expected) or set(pairs) != expected:
        raise ValueError("Rescoring requires one complete result per synthetic case and mode")
    if report.get("kind") != "synthetic_literal_evidence_ablation":
        raise ValueError("Only this probe's synthetic report can be rescored")
    for item in report["results"]:
        metrics = measure(cases[item["case"]], item["rows"])
        item.setdefault("initial_literal_measure", {key: item[key] for key in metrics})
        item.update(metrics)
    report.setdefault("initial_summary", report["summary"])
    report["summary"] = summarize(report["results"], len(cases))
    bodies = [row for row in report["memory_inventory"] if row["kind"] == "narrative"]
    report.setdefault("initial_authored_inventory_coverage", report["authored_inventory_coverage"])
    report["authored_inventory_coverage"] = {key: measure(case, bodies) for key, case in cases.items()}
    report["scoring_languages"] = ["en", "zh"]
    report["rescoring_note"] = "Added Chinese literal equivalents for public authored prose; model calls and returned rows are unchanged. Original measures are retained."
    return report


@contextmanager
def retrieval_mode(engine, mode, trace):
    # Evaluation-only ablation, one search at a time. Writer/tagger/profile and
    # raw-original fallback stay identical. Never force the model's selections.
    expand, entities, chooser = engine._EXPAND_ARCS, engine._EXPAND_ENTITIES, engine._arc_selections
    gaps = engine._EXPAND_GAPS
    planner = engine.arc_planning.choose
    engine._EXPAND_ARCS = mode != "base"
    engine._EXPAND_ENTITIES = False
    engine._EXPAND_GAPS = False  # This ablation isolates menus, not planned searches.

    def visible_menus(menus):
        return {key: {**menu, "materials": [item for item in menu["materials"]
                    if mode != "materials" or item["index"] != 0]} for key, menu in menus.items()}

    def choose(query, options, menus):
        visible = visible_menus(menus)
        choices = chooser(query, options, visible)
        trace.extend({"arc_key": key, "picks": picks,
                      "selected_ids": [item["id"] for item in visible[key]["materials"] if item["index"] in picks]}
                     for key, picks in choices)
        return choices

    def planned(model, query, options, evidence, menus):
        visible = visible_menus(menus)
        choices = planner(model, query, options, evidence, visible)
        trace.extend({**choice, "selected_ids": [item["id"] for item in visible[choice["arc_key"]]["materials"]
                                                if item["index"] in choice["picks"]]} for choice in choices)
        return choices

    engine._arc_selections = choose
    engine.arc_planning.choose = planned
    try:
        yield
    finally:
        engine._EXPAND_ARCS, engine._EXPAND_ENTITIES, engine._arc_selections = expand, entities, chooser
        engine.arc_planning.choose = planner
        engine._EXPAND_GAPS = gaps


def run(report_path, top_k):
    if os.environ.get("SEREIN_AML_PROFILE") != "development":
        raise ValueError("Quality probe requires an explicit development profile; mini is not used")
    if not os.environ.get("SEREIN_AML_MODEL_CONFIG"):
        raise ValueError("Quality probe requires an explicit public model-only configuration")
    flags = ["SEREIN_AML_" + flag for flag in ("ORGANIZE_ARCS", "WRITE_NARRATIVES", "TAG_MEMORIES", "EXPAND_ARCS")]
    previous_env = {key: os.environ.get(key) for key in [*flags, "SEREIN_AML_DATA_DIR"]}
    for key in flags:
        os.environ[key] = "1"
    work = tempfile.TemporaryDirectory(prefix="aml-synthetic-quality-")
    os.environ["SEREIN_AML_DATA_DIR"] = work.name
    # Import only after selecting the isolated data directory; restore the
    # engine root as well when invoked via runpy in an existing Python process.
    import aml.engine as engine
    from serein import model_runtime
    from serein.core.store import Store
    from serein.compat.narratives import narrative_transaction
    root, complete = engine._DATA_ROOT, model_runtime.complete
    engine._DATA_ROOT = Path(work.name)
    report = {"kind": "synthetic_literal_evidence_ablation", "profile": "development", "scoring_languages": ["en", "zh"],
              "transport": os.environ.get("SEREIN_AML_PROBE_TRANSPORT", "configured_api"),
              "top_k": top_k, "entity_expansion": False, "arc_planning": engine._PLAN_ARCS, "adds": [], "calls": [], "results": [],
              "limitations": ["Literal phrase coverage is not entailment or answer accuracy.",
                              "Unmatched records and distractor units are diagnostics, not precision scores.",
                              "Baseline includes searchable unfinished/unselected original messages.",
                              "All modes share one authored database; this isolates Search behavior, not Add cost.",
                              "Synthetic histories are not official AML questions."]}
    phase = "ingest"

    async def observe(model, payload, **kwargs):
        if model["model"].split("/")[-1].startswith("gpt-4o-mini"):
            raise ValueError("Mini is reserved for final acceptance")
        started = time.monotonic()
        print(json.dumps({"phase": phase, "model": model["model"], "call": len(report["calls"]) + 1}), flush=True)
        try:
            return await complete(model, payload, **kwargs)
        finally:
            report["calls"].append({"phase": phase, "model": model["model"], "seconds": round(time.monotonic()-started, 2)})

    model_runtime.complete = observe
    started = time.monotonic()
    success = False
    try:
        sessions, cases = fixture()
        report["source_messages"] = sum(len(messages) for _, _, messages in sessions)
        report["source_characters"] = sum(len(message["content"]) for _, _, messages in sessions for message in messages)
        for date, session, messages in sessions:
            stamp = int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000)
            before = time.monotonic()
            engine.add_memory(request_id=session, user_id="synthetic-quality-user", session_id=session,
                              messages=[{**message, "timestamp": stamp + index * 60000}
                                        for index, message in enumerate(messages)])
            report["adds"].append({"session": session, "seconds": round(time.monotonic()-before, 2)})
            print(json.dumps({"added": session}), flush=True)
        paths = engine._paths("synthetic-quality-user")
        with Store(paths.database, read_only=True) as store:
            documents = list(store.conn.execute("SELECT id,kind FROM documents WHERE lifecycle='active'"))
            report["memory_inventory"] = [{"id": row["id"], "kind": row["kind"],
                                          "content": store.read(row["id"])["body_md"]} for row in documents]
        kinds = {row["id"]: row["kind"] for row in documents}
        report["memory_counts"] = {kind: sum(value == kind for value in kinds.values()) for kind in set(kinds.values())}
        with narrative_transaction(paths.database) as rolls:
            bodies = [{"id": row["narrative_id"], "content": row["body"]} for row in rolls._load() if row["body"].strip()]
        report["authored_inventory_coverage"] = {case["id"]: measure(case, bodies) for case in cases}
        for case in cases:
            for mode in ("base", "materials", "volumes"):
                phase = case["id"] + ":" + mode
                trace = []
                before = time.monotonic()
                with retrieval_mode(engine, mode, trace):
                    rows = engine.search_memory(query=case["query"], options=None,
                                                user_id="synthetic-quality-user", top_k=top_k)
                result = {"case": case["id"], "tags": case["tags"], "mode": mode, **measure(case, rows),
                          "seconds": round(time.monotonic()-before, 2), "selections": trace,
                          "rows": [{**row, "kind": kinds.get(row["id"], "original")} for row in rows]}
                report["results"].append(result)
                print(json.dumps({key: result[key] for key in ("case", "mode", "missing_units", "context_characters", "seconds")}), flush=True)
        phase = "isolation"
        isolation = engine.search_memory(query=cases[0]["query"], options=None, user_id="empty-other-user", top_k=top_k)
        report["guards"] = {"other_user_empty": isolation == [],
                            "result_cap": all(item["returned_records"] <= min(top_k, engine._RETURN_CAP) for item in report["results"]),
                            "context_cap": all(item["context_characters"] <= engine._CONTEXT_CHAR_CAP for item in report["results"])}
        report["summary"] = summarize(report["results"], len(cases))
        # Missing evidence is a measured result, not a crash; preserve it in the
        # report rather than retrying models until coverage looks better.
        success = all(report["guards"].values())
    except Exception as error:
        report["error"] = {"phase": phase, "type": type(error).__name__}
        raise
    finally:
        report["completed"] = success
        report["elapsed_seconds"] = round(time.monotonic()-started, 2)
        model_runtime.complete = complete
        engine._DATA_ROOT = root
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        gc.collect()
        work.cleanup()
        print(json.dumps({"report": str(report_path), "completed": success}), flush=True)
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path(".local/quality-report.json"))
    parser.add_argument("--rescore", type=Path, help="Recheck an existing complete synthetic report without model calls")
    parser.add_argument("--top-k", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.top_k <= 100:
        parser.error("top-k must be between 1 and 100")
    if args.rescore:
        report = rescore(json.loads(args.rescore.read_text(encoding="utf-8")))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        raise SystemExit(0 if run(args.report.resolve(), args.top_k) else 1)
