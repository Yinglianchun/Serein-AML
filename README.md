# Serein-AML

Serein adapted for the **Agent Memory Leaderboard (Cycle 2, Textual Memory)**.

This repository imports the published Serein backend from commit `b5b13800ad086ea763afd9008ef3bdbfbb83c75b` (public release `0.1.0-rc65`) and keeps the competition changes separate and auditable.

## Public backend baseline

The 232 files under `src/serein/` match that fixed upstream commit. The import includes Event continuation and evidence handling, recoverable processing stages, grounded narrative candidate discovery, diary search, and separate Event/Scene domain rules. The import scope and selected upstream regression tests are recorded in [`upstream-serein.json`](upstream-serein.json).

The cached-image transcription regression is aligned with the current append-only writer contract: an extension reads new sources, while prior image transcriptions remain cached and bound to the existing Event.

AML Search calls the same public `Services.recall` entry point as MCP `recall_memory`, with explicit `mode="lookup"` and `method="lexical"`. It does not enable semantic/vector retrieval or the automatic chat routing and reranking path. This repository carries the backend needed by the API; the Serein web application remains in the original project.

## What changes for AML

The normal Serein chat path chooses when memories should surface, limits delivered cards, and suppresses repeated delivery. AML supplies explicit retrieval questions and scores answers from the returned evidence, so this adapter selects lookup mode.

The AML adapter adds competition orchestration around the public recall service:

- isolates storage physically by exact `user_id`;
- persists every Add request synchronously before returning HTTP 200;
- keeps the raw source conversation in the canonical Serein Event body and source binding;
- uses **gpt-4o-mini** during Add to derive grounded retrieval notes and named entities;
- uses **gpt-4o-mini** during Search only for query rewriting / bridge discovery, never to answer the benchmark question;
- retrieves through the public recall service, preserving Event/Scene domain rules, canonical readability, revision checks, and stale-index protection;
- performs one bounded round of entity bridge expansion through that same service;
- optionally shows body-free Narrative menus to the retrieval model and explicitly reads only its chosen materials (at most three menus and five items total);
- does not apply Serein's normal route-skip, cooldown, or two-card surfacing cap;
- bounds the total returned memory text by a character budget, reserving space for every selected record; long records may be returned as prefix excerpts;
- returns the fixed AML `{"data": [...]}` schema and never exceeds `top_k`.

MCP and automatic recall share the `typed_memory` card and Narrative menu renderer, but their envelopes and selection rules differ. MCP adds versions, comments and optional evidence; automatic recall wraps the selected context for chat delivery. AML keeps its required `data` array and returns the selected canonical memory bodies, rather than tool-call instructions or a generated answer.

Narrative expansion is opt-in. It does not create volumes during Add: existing volume memberships are required. The current Add path still creates Events only, so enabling expansion alone does not give a fresh evaluation database any Narrative volumes. Synthetic fixtures test the read path; automatic volume organization is separate work.

## API

- `GET /health` — unauthenticated health endpoint.
- `POST /add` — AML synchronous Add.
- `POST /search` — AML Search.

Authentication supports:

- `Authorization: Bearer <key>`
- `Authorization: Token <key>`
- `X-Api-Key: <key>`

Set `SEREIN_AML_API_KEY` to require authentication. If it is empty, the service is open for local/public smoke testing.

## Run

```bash
docker build -f Dockerfile.aml -t serein-aml .
docker run --rm -p 8000:8000 \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e SEREIN_AML_API_KEY="$SEREIN_AML_API_KEY" \
  -v "$PWD/.aml-data:/data" \
  serein-aml
```

The open-source AML method fixes its Add/Search LLM to `gpt-4o-mini`. For OpenRouter, set `OR_key` in the process environment instead of `OPENAI_API_KEY`; the adapter selects `https://openrouter.ai/api/v1` and sends the required `openai/gpt-4o-mini` identifier. Alternatively, set `OPENAI_API_KEY` and `OPENAI_BASE_URL` explicitly. Credentials are never read from memory records or committed configuration. Responses are stateless (`store=false`).

Optional:

```bash
-e SEREIN_AML_RETURN_CAP=40
-e SEREIN_AML_CONTEXT_CHAR_CAP=24000
-e SEREIN_AML_EXPAND_ARCS=1
```

The formal AML request may use `top_k=100`; the local return cap can be tuned up to 100 while always respecting the requested maximum. The context cap counts characters across all returned `content` fields, not tokens. Set `SEREIN_AML_EXPAND_ARCS=1` to enable menu selection; it is disabled by default and never reads a whole volume implicitly. The retrieval model may decline to expand any menu. Invalid or changed selections are ignored.

## Test

```bash
python -m pip install '.[http,background,test]'
pytest -q tests_aml tests
```

## Data handling

Evaluation data is stored under `SEREIN_AML_DATA_DIR`, with one hashed directory per exact `user_id`. `session_id` is retained only as source provenance and is never used as a Search isolation filter.

Do not use AML evaluation payloads for training, fine-tuning, dataset reconstruction, or unrelated analytics. Delete evaluation data within the retention period required by the competition.

## Attribution

Original project: [Yinglianchun/Serein](https://github.com/Yinglianchun/Serein), same maintainer/team.

Current imported baseline: [`b5b13800ad086ea763afd9008ef3bdbfbb83c75b`](https://github.com/Yinglianchun/Serein/tree/b5b13800ad086ea763afd9008ef3bdbfbb83c75b).

Original bootstrap baseline: `0bc2c64cf382193cd959c5e29ca1131a78297365`.

AML-specific changes live in `aml/`, `Dockerfile.aml`, `requirements-aml.txt`, and `tests_aml/`. The pytest settings in `pyproject.toml` include both regression suites and make the root-level AML package importable; CI runs both suites.

Official competition/API documentation: https://agentmemoryleaderboard.ai/
