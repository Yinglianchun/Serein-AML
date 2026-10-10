# AML delivery guards (opt-in)

## Existing Narrative read path

Narrative authoring, Arc menu selection (index 0 for authored prose), joint
review, and final Search delivery already support dated stages and causal prose.
`tests_aml/test_narrative_history_delivery.py` exercises that chain with a
synthetically authored volume and real storage/reader/Search code; model
decisions are fixtures, not real-mini acceptance or a benchmark reproduction.

Final excerpt diagnostics alone do not cover earlier clipping. Previously the
joint-review stage shared 8000 characters among pending reads, including anchor
text; a synthetic long volume lost its later causal facts before review.
That shared character cap is now removed. Selected material bodies remain whole
and are greedily packed into review calls bounded by SEARCH_INPUT_BYTES (default
24000 UTF-8 bytes), including instructions, question, options, current evidence,
anchors and JSON overhead. Multiple materials may require multiple calls. A
single material that cannot fit is skipped with INPUT_BUDGET, not silently cut;
other fitting materials can still be reviewed. No retry or source reread is
added. The initial planning-evidence 8000-character cap and final output cap
are unchanged. Tests verify that a formerly truncated volume's later facts reach
both review and final Search. This is not proof of a benchmark improvement.

With DELIVERY_AUDIT enabled, `arc_picks` records the number of menu choices,
`arc_read` records offered body length, `arc_read_rejected` records INPUT_BUDGET,
and `arc_review` records the accepted count. References remain random and
request-local; no titles, queries, bodies or model responses are logged. A zero
accepted count does not distinguish semantic rejection from invalid model
format. Tests use synthetic model decisions and do not establish real-mini
semantic acceptance.

## Literal excerpts without model selection

`SEREIN_AML_BALANCED_EXCERPTS=1` (default `0`) changes only final legacy
excerpt delivery when both `SOURCE_EVIDENCE` and `DELIVERY_FIXES` are off.
It keeps the existing per-record allocation and total character budget; short
bodies remain unchanged. Reviewed literal focus quotes have priority. Remaining
space covers the beginning, end, and a query-matching interior window, with the
midpoint as fallback. Matching uses literal English words and Chinese bigrams,
not model decisions, inferred causal relations, or current-state judgments.
Windows are merged and returned in source order, separated by `[…]` where
material was omitted. Gap marks count against the budget. An exact-sized focus
quote reservation is returned verbatim when marks do not fit; a tiny slot with
no focus may return nothing. Windows may contain partial sentences.

This does not enable original-source reads, time recall, timeline construction,
new candidate expansion, or another model call. It does not guarantee complete
evidence, chronological order, privacy classification, or benchmark improvement.
It prevents the default prefix-only bias, but literal matching can still miss
relevant middle facts. When existing anonymous audit is enabled, truncated
records emit `PARTIAL_EXCERPT` with a request-local reference and output length,
never query or body content. No production flags are enabled by this code change.

Focused checks: `python -m pytest tests_aml/test_balanced_excerpts.py tests_aml/test_bridge_retention.py tests_aml/test_delivery.py tests_aml/test_contract.py -q`.

`SEREIN_AML_DELIVERY_FIXES=1` enables only the guarded delivery path. Its default is `0`, preserving the existing baseline. With `SEREIN_AML_SOURCE_EVIDENCE=0`, it selects bounded units from the existing candidate bodies; it does not add time candidates, request bound originals, normalize dates, or classify historical/current state. Existing pending-original candidates remain available through their existing recall route. Source bindings are still read locally for visibility/version checks, without sending extra originals to the selector.

`SEREIN_AML_SOURCE_EVIDENCE=1` remains the separate older experimental package. Combining it with delivery guards also guards its existing source-selection path; it is not necessary for testing body delivery. The final selector enforces its existing UTF-8 input budget. Enabling delivery alone does not change query rewriting or relationship-review prompts, redaction, or their upstream input limits. No flags are enabled by this change.

Guard behavior:

- Unit IDs accept original valid strings and non-negative JSON integers. Booleans, floats, negatives, unknown IDs and duplicates after normalization are rejected.
- Model format errors, model call failures and invalid selections raise `DeliveryError` with a closed reason vocabulary. HTTP returns 503 with that safe reason, rather than a successful empty match. `selections: []` remains a normal empty match. There is no full-body fallback or repair loop.
- Top-k reserves complete previously validated groups. Ordinary fill cannot reintroduce an isolated expansion. A direct hit retains its independent eligibility even when it also belongs to a group.
- Output budgets reserve whole selected excerpts for groups and charge actual physical carriers once. If a group cannot fit, its expansions are omitted; direct hits remain eligible within the budget. Whole selected units are preserved instead of silently losing selected qualification or relation units. This conservative policy can return fewer results than a fragment-based policy.
- Group endpoints keep their own physical carriers unless existing reviewed `focus` quotes for every endpoint are covered by selected units on one endpoint carrier, and those units occur in each endpoint's actually offered catalog with identical source, text, speaker, message time, time origin and date notes. Complete source snapshots must match and remain current. The proof is scoped to that group; it cannot be borrowed by an overlapping group. A shared source ID, an unrelated shared sentence, body-only text or keyword overlap cannot establish this equivalence. No unselected unit is added. Non-group exact source/text duplicates retain their existing request-local behavior.
- Final filtering rechecks proof participants' document/source snapshots and narrative visibility, including unselected endpoints. Invalid physical proof carriers are removed even when also direct hits; an unchanged direct hit retains independent eligibility when a different endpoint invalidates the group. Final filtering repeats until removing an expansion cannot leave another accepted group dependent on that removed carrier. An empty mapping never falls back to fabricated self-carriers. Allocation now emits fixed `INCOMPLETE_GROUP` audit reasons when it excludes an incomplete group/orphan.
- These checks describe physical delivery, not entailment. The existing relation checks and the selector's necessary semantic judgment remain necessary; record presence or a delivery count does not establish adequate evidence or causal correctness.

`SEREIN_AML_DELIVERY_AUDIT=1` separately enables diagnostics; its default is `0`. Events cover found candidates, offered units, model selections, program acceptance/rejection, input/output budgets, deduplication, group checks and final rows. Logs contain only a random per-request token, random per-request material aliases, fixed stage/reason codes, counts and character lengths. Input budgets continue to use UTF-8 bytes. The in-memory alias map is discarded on request exit, including failures. There are no real IDs, queries/options, body text, source text, answers, model raw output or content hashes in these events. Default operation writes no additional audit events. When explicitly enabled, audit events use WARNING level to survive the service default threshold without raising global log verbosity. Tests can replace the event sink for synthetic diagnostics.

Known limits: first/last unit windows, oversized-unit dropping and local source-binding windows remain unchanged. Semantic sufficiency is not automatically certified. Group duplicates may consume more output space; omission is safer than inventing support from unrelated shared text. Earlier synthetic contest-mini validation on 2026-10-08 comprised an initial 12-call Responses batch and a separate 8-call active-database batch. The second batch used the real configured task_model/complete adapter with JSON response format and an explicit test-only max_tokens=1800 (the production active branch has no output cap). After clarifying that linked_refs are not selectable unless offered as EVIDENCE records, final-prompt output-budget/top-k/no-match/minimum-disclosure cases passed. The shared-source case retained both physical carriers but omitted the necessary employment fact; it did not pass semantic acceptance. The cross-Event case passed before the last prompt clarification and was not rerun afterward. Controlled recall/relationships and offline order replay do not establish end-to-end retrieval quality or score improvement. The separate local reports preserve failures and request differences. No official data or deployment was used.

The latest factual-chain prompt was then retested in a fresh 8-call active-database batch. Both shared-source calls selected the employment and city facts, but only one delivered them: the other selected only expansion carrier b, and the existing complete-group allocation excluded it because direct carrier a was not selected. Cross-Event, no-match, minimum disclosure, output budget, top-k and explicit absence of a causal link passed on this latest prompt. This 7/8 outcome is not complete acceptance; the remaining failure is selected evidence removed by the existing group policy, rather than omission of the intermediate fact by the model. No algorithm or gate was changed during the retest. The request budget and test-only output-cap difference remain as described above; the separate chain report preserves real outputs and zero-call diagnosis.

A later offline-only program fix adds the strictly scoped shared projection described above. The original chain fixture had no reviewed focus and still declines; the same saved b-only selection delivers both facts only in a separately labelled controlled review-focus fixture shaped like production entity_relation/reviewed planning groups. Production entity_relation groups carry focus; planned_gap and reviewed arc_menu groups receive focus from the existing review; unplanned arc_menu groups may lack it and remain conservative. Quote coverage proves the reviewed fragments are physically present, not that the prior review is semantically correct or that arbitrary causal reasoning is complete. That offline fix stage did not include new model calls; historical mini failures are preserved.


A subsequent fresh 7-call mini batch passed on the latest shared-carrier code through the real engine body path (no _read_refs/source requests), including two controlled reviewed-focus shared chains, cross-Event, top-k, output budget, no-match and minimum disclosure. The shared cases selected both records and returned one carrier with both required facts; mini did not naturally emit b-only in this batch. The b-only behavior remains supported by the separate saved-output offline replay, and missing-fact/no-focus refusal by zero-call fixtures, not additional natural model samples. Review focus and recall/source binding were controlled synthetic fixtures, not a relation-model or end-to-end retrieval acceptance. Actual HTTP JSON was bounded at 24000 UTF-8 bytes with test-only max_tokens=1800. This batch did not deploy anything or establish score improvement.

Focused regression: `python -m pytest tests_aml/test_delivery.py tests_aml/test_shared_delivery.py tests_aml/test_source_evidence.py tests_aml/test_bridge_retention.py tests_aml/test_entity_bridge.py tests_aml/test_contract.py -q`.
