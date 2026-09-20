# Serein-AML

Serein adapted for the **Agent Memory Leaderboard (Cycle 2, Textual Memory)**.

This repository starts from Serein commit `0bc2c64cf382193cd959c5e29ca1131a78297365` and keeps the competition changes separate and auditable.

## What changes for AML

The normal Serein chat path is intentionally conservative: routing may skip recall, surfacing is capped, cooldown applies, and relation expansion is small. Those are good product behaviors but poor leaderboard retrieval defaults.

The AML adapter therefore uses a separate path:

- isolates storage physically by exact `user_id`;
- persists every Add request synchronously before returning HTTP 200;
- keeps the raw source conversation in the canonical Serein Event body and source binding;
- uses **gpt-4o-mini** during Add to derive grounded retrieval notes and named entities;
- uses **gpt-4o-mini** during Search only for query rewriting / bridge discovery, never to answer the benchmark question;
- retrieves through Serein's canonical SQLite store plus FTS index;
- performs controlled entity bridge expansion for cross-fragment / multi-hop evidence;
- does not apply Serein's normal route-skip, cooldown, or two-card surfacing cap;
- returns the fixed AML `{"data": [...]}` schema and never exceeds `top_k`.

This is an initial competition baseline. Semantic/vector retrieval and stronger relation composition can be added after Smoke results without changing the public API contract.

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

The open-source AML method fixes its Add/Search LLM to `gpt-4o-mini`.

Optional:

```bash
-e SEREIN_AML_RETURN_CAP=40
```

The formal AML request may use `top_k=100`; the local return cap can be tuned up to 100 while always respecting the requested maximum.

## Test

```bash
python -m pip install '.[http,background,test]'
pytest -q tests_aml
```

## Data handling

Evaluation data is stored under `SEREIN_AML_DATA_DIR`, with one hashed directory per exact `user_id`. `session_id` is retained only as source provenance and is never used as a Search isolation filter.

Do not use AML evaluation payloads for training, fine-tuning, dataset reconstruction, or unrelated analytics. Delete evaluation data within the retention period required by the competition.

## Attribution

Original project: [Yinglianchun/Serein](https://github.com/Yinglianchun/Serein), same maintainer/team.

Imported baseline: `0bc2c64cf382193cd959c5e29ca1131a78297365`.

AML-specific changes live in `aml/`, `Dockerfile.aml`, `requirements-aml.txt`, and `tests_aml/`.

Official competition/API documentation: https://agentmemoryleaderboard.ai/
