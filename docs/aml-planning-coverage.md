# AML planning coverage

`SEREIN_AML_PLANNING_COVERAGE=1` opts into bounded planning coverage repair.
Default: `0`. It requires an existing Arc-planning or gap-search route; it does
not enable those routes itself.

Previously, gap retrieval discarded every existing candidate, even when the
planner had not seen it among its eight excerpts or had only seen a truncated
excerpt. With this switch enabled, such a candidate can enter the existing
joint review once. A fully shown candidate, the anchor itself, or a material
already scheduled through an Arc menu cannot enter this reread path. Revisions,
pending-original content, readability and domain restrictions remain checked.
Accepted existing gap candidates keep their original retrieval ranking: rereading
does not add a score bonus, reserve candidate/result slots, or move the pair to
the front. Joint-review focus and source validation remain available when the
materials fit the result. Rejection does not remove independent direct-retrieval
eligibility. Newly found gap material still uses relation-group retention.
When top_k cannot hold both existing endpoints and higher-ranked direct evidence,
the original ranking wins; this reread does not promise both endpoints will fit.

Arc menus are seeded from the top eight canonical candidates after reranking,
instead of the first six candidates before reranking. Menus are ordered by
their best seed and then stable Arc key. The existing three-menu limit remains.

The planner still receives at most eight evidence excerpts within its existing
16,000-character total budget (also bounded by `SEREIN_AML_CONTEXT_CHAR_CAP`).
This excerpt-budget increase applies independently of the coverage switch;
it was previously 8,000 characters. There is still one gap-search round, up to two
grounded queries, and at most one gap candidate for joint review. This is not a
second planning round or exhaustive traversal. Candidates must still be found
by the grounded gap query; unseen material is not automatically read. Existing
model-input and response budgets remain in force. Additional review calls and
changed result priority are possible when the switch is enabled.

`gap_existing_unseen` records only the number of existing eligible candidates
before the subsequent current-read guards, when delivery audit is enabled. It
does not certify final delivery or semantic sufficiency. No query or body is
added to diagnostics.

Synthetic regressions cover default-off behavior, unseen and truncated existing
candidates, fully seen and already scheduled exclusions, changed revisions,
one joint review without duplicate delivery, and menus beyond the original
pre-rerank seeds. They do not establish mini performance or official scores.
