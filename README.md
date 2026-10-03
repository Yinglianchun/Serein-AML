# Serein-AML

Serein adapted for the **Agent Memory Leaderboard (Cycle 2, Textual Memory)**.

This repository imports the published Serein backend from commit `b5b13800ad086ea763afd9008ef3bdbfbb83c75b` (public release `0.1.0-rc65`) and keeps the competition changes separate and auditable.

## Public backend baseline

The 232 files under `src/serein/` match that fixed upstream commit. The import includes Event continuation and evidence handling, recoverable processing stages, grounded narrative candidate discovery, diary search, and separate Event/Scene domain rules. The import scope and selected upstream regression tests are recorded in [`upstream-serein.json`](upstream-serein.json).

The cached-image transcription regression is aligned with the current append-only writer contract: an extension reads new sources, while prior image transcriptions remain cached and bound to the existing Event.

AML Search calls the same public `Services.recall` entry point as MCP `recall_memory`, with explicit `mode="lookup"`. With a selected embedding model, it queries the original question semantically and supplements it with lexical searches. The public reranker orders the combined evidence. This repository carries the backend needed by the API; the Serein web application remains in the original project.

## What changes for AML

The normal Serein chat path chooses when memories should surface, limits delivered cards, and suppresses repeated delivery. AML supplies explicit retrieval questions and scores answers from the returned evidence, so this adapter selects lookup mode.

The AML adapter adds competition orchestration around the public recall service:

- isolates storage physically by exact `user_id`;
- persists every Add request synchronously before returning HTTP 200;
- archives exact user/assistant source messages, roles and supplied timestamps in the public original archive;
- runs the public **Track Router → Event Curator → Event Writer** pipeline, including its source ownership, exact-quote and settlement validations;
- uses **gpt-4o-mini** for every generative role in the competition profile, including Scout and Search helpers, never to answer the benchmark question;
- permits explicitly configured other models for development in a separate fresh database;
- retrieves through the public recall service, preserving Event/Scene domain rules, canonical readability, revision checks, and stale-index protection;
- prepares the selected public embedding profile, routes and vector/passage indexes, and synchronously fills new coverage;
- retains incomplete and Curator-unselected dialogue as searchable original evidence, with disposable vectors using that same embedding profile; settled Event sources are excluded from this fallback;
- orders evidence with the configured public RerankerClient, preserving lookup admission rather than applying automatic surfacing thresholds;
- optionally drains the public metadata tagger after Event settlement and refreshes entity/cue indexes before organization and Add completion;
- optionally runs the public automatic Arc Scout after ingestion, creating collecting lines or appending material links to existing Arcs;
- optionally authors those volumes from their bound sources through the public read/preview/save workflow, before Add succeeds;
- optionally shows body-free Narrative menus to the retrieval model and explicitly reads only its chosen materials (at most three menus and five items total);
- retains each admitted Arc selection together with its direct anchor within the final result cap, rather than dropping the bridge on its individual similarity to the original question;
- optionally tries one round of entity-plus-relationship retrieval from up to five direct hits, trying at most three bridge entities and adding at most one related candidate;
- does not apply Serein's normal route-skip, cooldown, or two-card surfacing cap;
- bounds the total returned memory text by a character budget, reserving space for every selected record; long records may be returned as prefix excerpts;
- returns the fixed AML `{"data": [...]}` schema and never exceeds `top_k`.

MCP and automatic recall share the `typed_memory` card and Narrative menu renderer, but their envelopes and selection rules differ. MCP adds versions, comments and optional evidence; automatic recall wraps the selected context for chat delivery. AML keeps its required `data` array and returns the selected canonical memory bodies, rather than tool-call instructions or a generated answer.

Both automatic organization and menu expansion are opt-in. `SEREIN_AML_ORGANIZE_ARCS=1` enables the public Scout on the isolated benchmark database after each Add; the nightly wall-clock delay is bypassed for synchronous ingestion. Scout reads bounded public candidates and may decline to group them. It creates empty collecting lines and material relationships, preserving the public boundary that automatic organization does **not** author Narrative prose. Existing authored volumes retain their preview/save contract. `SEREIN_AML_EXPAND_ARCS=1` lets Search choose and read related materials from these menus, so a new evaluation database can exercise real automatic grouping without manually supplied themes.

`SEREIN_AML_WRITE_NARRATIVES=1` adds a separate benchmark authoring step after Scout and enables organization automatically. It uses the selected `writer` model (the development assignment or competition mini) to write coherent prose from the public frozen source snapshot. Bound original messages are preferred to derived Event/Scene summaries. Each paragraph carries validated exact source refs and quotes in the local authoring receipt; the model remains responsible for semantic faithfulness. All bound materials must be represented. The existing Narrative `read → preview → save` checks still validate source snapshots, document hashes and revisions before publishing the exact draft. Scout remains material-only, and the imported public backend is unchanged.

New collecting volumes are authored; previously AML-authored volumes are rewritten from all current bound sources when those sources or the topic change. This uses `rewrite`, because Scout has already appended new membership before Writer runs. Unchanged inputs and bodies skip generation; independently authored or manually edited prose is preserved. Pending collecting volumes are revisited even when Scout now reports unchanged. The published revision and AML acknowledgment commit together, so a retry after a later index failure cannot publish twice. Add stays pending on writing/preview/save failure, and source conflicts require a fresh read and draft. Narrative prose is refreshed in the lexical index; the public vector/passage indexes cover Event and Scene. Search can select menu index 0 for a relevant completed Narrative, or select individual materials and decline irrelevant menus. Automatic writing and menu expansion are independently opt-in; use both to exercise the complete route.

Before returning a volume body, Search also checks that its current Event/Scene materials are readable and allowed for the original question. AML-authored prose must still match its committed source snapshot, selected membership, title/focus and body hash. Changed or newly linked materials suppress the old prose until it is rewritten; independently eligible individual materials remain available.

`SEREIN_AML_TAG_MEMORIES=1` runs the unchanged public metadata-tagging queue synchronously after Event settlement. The `operit_tagging` model suggests a primary domain and extracts named entities from exact bound originals, or explicitly labeled body-only material when no original is bound. Invalid entity refs/quotes are filtered by the public validator; a valid empty entity list is allowed. Authored titles, bodies, domains and cues are preserved. Eligible imported Scenes use the public cue-generation rules; no Event cues or new facts are invented. Aliases remain suggestions and never merge identities.

Tagging drains the public two-job batches until the active queue is complete. A paid request/output failure leaves Add pending without an automatic paid retry in that attempt. Explicitly retrying Add requeues failed automatic tag jobs while retaining their attempt history; successful metadata is reused after later index failures. Before Scout, the adapter refreshes canonical entity observations and, where semantic preparation supports it, public Event lexical and Scene cue bindings. Cue-binding failures retain the public durable pause rules and their status in the Add receipt. The public tagging model transport receives `store=false` only within the AML tagging/binding context; unrelated public calls retain their original payloads.

`SEREIN_AML_EXPAND_ENTITIES=1` enables a separate, bounded lookup route without another generative-model call. It reads current public entity extractions, revalidates their exact source quotes, and falls back to a few literal English-name, Chinese-organization and quoted-title rules when a new Event has no entity tags. Every bridge name must also occur in the delivered canonical anchor body. Suggested aliases are never merged. Source quotes keep `bound_source`, `memory_body` or `raw_original` provenance; changing the body or bindings invalidates the path. Nothing is written to public entity metadata or relationship tables.

The initial templates cover factory/origin, location, affiliation and date questions. A named bridge is combined with the requested relationship and explicit time hints; lexical/semantic recall supplies candidates, and the selected public reranker scores them against this bridge query. An exact shared name and a requested predicate must appear together in the candidate text. Unsupported questions or absent new evidence stop immediately, and expansion results never seed another hop. These checks establish a literal evidence connection, **not** entailment of the answer or proof that a historical affiliation still holds. Time conditions remain retrieval hints; old and conflicting evidence is not automatically suppressed.

Search tracks the anchor, bridge, query, exact support quotes and Arc fingerprint internally. After fresh canonical revision, visibility and original-question domain checks, complete groups receive slots within `min(top_k, SEREIN_AML_RETURN_CAP)`. Explicit Arc selections take precedence over the optional entity candidate. Failed paths cannot leak expansion-only records through ordinary ranking; independently retrieved records remain eligible. `top_k=1` cannot guarantee a two-record chain. The external AML schema remains unchanged. Within the character budget, fitting entity-chain quotes receive space before ordinary excerpts; long bodies are returned as literal excerpts, never generated summaries.

The adapter reuses the public Scout's inventory, source hydration, keyword/entity/semantic candidate builders, prompt, candidate normalizer and material-only writes. It adds up to three format/validation attempts before applying a decision: invented targets, missing seed materials or invalid references request a corrected reply rather than silently producing an empty grouping result. Raw Scout replies and validation failures are archived locally. A valid `candidates=[]` remains an ordinary decision and is never retried to force an Arc. Transport failures leave Add pending for a whole-request retry. Public retired-binding cleanup and stale authored-volume hints still run. Changing eligible Arc targets also invalidates the scan fingerprint.

AML does not exercise Serein's automatic decision to speak or stay quiet, repeated-delivery cooldown, or `resume` in a new chat. An ordinary conversational demonstration is needed to show those capabilities.

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

The default `competition` profile fixes every generative role to `gpt-4o-mini`. For OpenRouter, set `OR_key` in the process environment instead of `OPENAI_API_KEY`; the adapter selects `https://openrouter.ai/api/v1` and sends `openai/gpt-4o-mini`. Alternatively, set `OPENAI_API_KEY` and `OPENAI_BASE_URL` explicitly. Generation requests use `store=false`. Add uses the public stage-runner entry point and model transport, with a Curator structure reminder and at most three validation attempts. The adapter tolerates JSON fences, one-object wrappers, numeric strings in integer fields, and unused extra fields on `decision_review.events` activity reasons. Original replies remain archived. Source ownership, action/base cardinality, boundary/disposition evidence, exact quotes and settlement still pass unchanged public validation; missing IDs or evidence are never filled in by the adapter. Credentials belong to runtime settings, never committed configuration.

To exercise the complete semantic path, mount an explicitly exported **public model configuration** and set `SEREIN_AML_MODEL_CONFIG=/run/secrets/public-models.json`. It has Serein's `models`, `upstreams`, and `assignments` fields. Only selected model connections are copied to each isolated database; personal identity, memories, features and deployment settings are not imported. Assign `embedding` and `reranker` to the desired public providers. Their non-generative models stay selected in both profiles. Without this file the adapter can still use the public Event pipeline and lexical retrieval, but cannot claim semantic coverage.

For development, set `SEREIN_AML_PROFILE=development` and provide that file. The configured `track_router`, `event_curator`, `event_writer`, `writer`, `narrative_scout` and `operit_tagging` assignments are used; missing roles fall back to the selected Writer. Tagging may use a separately selected public provider. Development rejects `gpt-4o-mini`. Before the final local acceptance test, switch to `competition` and use a **new, empty** `SEREIN_AML_DATA_DIR`. Reusing a profiled database with different models is rejected by both Add and Search. Search checks the actual database model assignments and descriptors against the profile as well as its saved marker; changing them cannot silently cross profiles or reuse another embedding profile. A missing active Search model fails instead of falling back to mini. Development-generated memories cannot enter a competition run.

Optional:

```bash
-e SEREIN_AML_RETURN_CAP=40
-e SEREIN_AML_CONTEXT_CHAR_CAP=24000
-e SEREIN_AML_EXPAND_ARCS=1
-e SEREIN_AML_ORGANIZE_ARCS=1
-e SEREIN_AML_WRITE_NARRATIVES=1
-e SEREIN_AML_TAG_MEMORIES=1
-e SEREIN_AML_EXPAND_ENTITIES=1
```

The formal AML request may use `top_k=100`; the local return cap can be tuned up to 100 while always respecting the requested maximum. The context cap counts characters across all returned `content` fields, not tokens. Set `SEREIN_AML_EXPAND_ARCS=1` to enable menu selection; it is disabled by default and never reads a whole volume implicitly. The retrieval model may decline to expand any menu. Invalid or changed selections are ignored.

Writing and tagging are part of the database profile. Use a fresh empty data directory when enabling or disabling `SEREIN_AML_WRITE_NARRATIVES` or `SEREIN_AML_TAG_MEMORIES`; neither Add nor Search can silently switch an existing database across that boundary.

Semantic lookup uses the public theme-discovery candidate floor of 0.3 cosine. Final reranking considers at most 100 admitted candidates, including explicitly chosen Arc materials. This is a candidate threshold, not a calibrated relevance claim. Preparing routes and vectors for the first Add can be slow; provision caller timeouts accordingly. Add remains pending if the Event pipeline, metadata tagging, index fill, enabled Scout or authoring fails, and the same `request_id` can be retried without duplicating archived originals.

## Test

```bash
python -m pip install '.[http,background,test]'
pytest -q tests_aml tests
```

The optional synthetic quality probe compares base lookup, menus restricted to
individual materials, and menus that also offer completed Narrative bodies on
the same newly ingested database. It uses six invented sessions (31 raw dialogue
messages), including employment and premises updates across sessions, namesake
confusion, retracted explanations, corrected dates, incidental details, and an
unfinished original. No prewritten Serein memories or benchmark answers are
provided to Add.

```bash
SEREIN_AML_PROFILE=development SEREIN_AML_MODEL_CONFIG=/path/to/public-models.json \
  python -m evals.quality --report .local/quality-report.json --top-k 4
```

The probe requires explicitly selected non-mini development models. It creates
and deletes its own temporary database, and enables tagging, Scout and Narrative
writing before ingestion. Entity expansion is disabled in all three comparison
modes to isolate menu behavior; the baseline retains the normal original-message
fallback. Actual menu choices remain model decisions. Questions and expected
evidence labels are never added to memory; labels are used only by local scoring
after Search returns. The report records missing evidence units, selected IDs,
returned synthetic text, distractor matches, character use, calls and duration.
Phrase coverage is a diagnostic, not semantic entailment, answer accuracy or an
official AML score. Missing evidence is reported without rerunning models to
force a better result. API failures stop the run and leave a partial report.
This small probe does not establish large-history throughput or mini quality.

## Data handling

Evaluation data is stored under `SEREIN_AML_DATA_DIR`, with one hashed directory per exact `user_id`. `session_id` is retained only as source provenance and is never used as a Search isolation filter.

Do not use AML evaluation payloads for training, fine-tuning, dataset reconstruction, or unrelated analytics. Delete evaluation data within the retention period required by the competition.

## Attribution

Original project: [Yinglianchun/Serein](https://github.com/Yinglianchun/Serein), same maintainer/team.

Current imported baseline: [`b5b13800ad086ea763afd9008ef3bdbfbb83c75b`](https://github.com/Yinglianchun/Serein/tree/b5b13800ad086ea763afd9008ef3bdbfbb83c75b).

Original bootstrap baseline: `0bc2c64cf382193cd959c5e29ca1131a78297365`.

AML-specific changes live in `aml/`, `Dockerfile.aml`, `requirements-aml.txt`, and `tests_aml/`. The pytest settings in `pyproject.toml` include both regression suites and make the root-level AML package importable; CI runs both suites.

Official competition/API documentation: https://agentmemoryleaderboard.ai/
