# External observations: intake contract, gate policy and decisions

This document describes the external-observation path added to the competition pipeline on
2026-10-02. It is the interface between connector output and the published company profile.

Code:

| Concern | Module |
| --- | --- |
| intake, identity, duplicates, freshness scope, run summary | `src/norway_company_agent/external_pipeline.py` |
| publication policy and aggregation | `src/norway_company_agent/external_footprint.py` |
| contract serialisation and validation | `src/norway_company_agent/batch.py` |
| CLI wiring | `scripts/run_competition_batch.py --external-observations` |
| audit evaluator (scorer inputs) | `scripts/evaluate_external_footprint.py` |
| end-to-end verification | `scripts/verify_competition_e2e.py` |

## Record schema (JSONL, one object per line)

The field names are the ones the existing connectors already emit
(`scripts/extract_company_site_news.py`, `scripts/extract_company_site_activity.py`,
`scripts/build_verified_observations.py`, the Places/LinkedIn/YouTube connectors and the sentiment
model). The gate consumes them; it does not rename them.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `id` | string | yes | Unique per organisation (not globally). |
| `organisation_number` | string | yes | 9 digits; must match the organisation whose profile is built. |
| `platform` | string | yes | One of `external_footprint.PLATFORMS`. |
| `signal_type` | string | yes | One of `external_footprint.SIGNAL_TYPES`. |
| `source_url` | string | yes | Must parse to a hostname. |
| `retrieved_at` | string | yes | Must be a string; an unparseable string is accepted but never fresh (see freshness below). |
| `content_sha256` | string | yes | 64 lowercase hex characters. See "hash limitation". |
| `exact_entity` | bool | yes | Must be `true`. |
| `identity_proof` | list | yes | Non-empty list of objects; other value types are rejected. |
| `acquisition_mode` | string | yes | Must be in `PUBLISHABLE_ACQUISITION_MODES`; experimental modes never publish. |
| `rights_status` | string | yes | Must be `"approved"`. |
| `source_class` | string | recommended | Required for independent sentiment classes. |
| `evidence_span` | string | required for `review`, `public_post`, `public_mention` | The quoted support. |
| `sentiment_label`, `sentiment_model_version` | string | optional pair | A label without a model version is rejected. |
| `reviewer_id` | string | optional | Enables the reviewer-based sentiment gate. |
| `metrics` | object | optional | Source-specific counters. `likes`, `comments` and `shares`, when present, must be non-negative finite numbers; values are never coerced. |

## Pipeline order

1. **Parse** (`read_observation_file`): malformed lines are collected with file and line number and
   never abort the batch. An unreadable file is an operator error and stops the run.
2. **Identity and duplicates** (`deduplicate_observations`): identity is scoped to
   `(organisation_number, id)`. Identical duplicates collapse to the first occurrence; conflicting
   duplicates (same identity, different payload) are withheld entirely and reported; identical
   payloads with different ids are reported as content duplicates but not dropped, because distinct
   ids may be distinct captures and dropping one would lose data. The same id for two organisations
   is normal and is never treated as a collision.
3. **Entity binding** (`validate_observation(..., organisation_number=...)`): a record whose
   `organisation_number` is not exactly nine ASCII digits is a data error and is rejected with
   `missing or invalid organisation number` (never silently coerced from an integer, and never
   counted as "outside the batch"). A well-formed organisation number that is not in the requested
   batch is reported as `unmatched` and never touches another profile.
4. **Rights and policy**: unapproved rights, experimental acquisition modes, unsupported platforms,
   missing spans, unverified exact-entity flags and malformed hashes are rejected with
   machine-readable reasons.
5. **Freshness**: acceptance and freshness are separate signals. `accepted_observations` counts the
   publication decision; `fresh_observations` / `stale_observations` / `undated_observations` count
   the freshness window relative to `--external-as-of` (default: wall clock) and
   `--external-freshness-days` (default: 45). Undated records are never fresh. With
   `--enforce-freshness` the counters, the sentiment gate and the sentiment score use fresh records
   only; stale records stay visible in `stale_observation_ids`. The scope used is always recorded in
   `counts_scope` and `freshness_policy`.
6. **Aggregation** (`aggregate_footprint`): one call over the accepted records for the batch and one
   per organisation, so the per-organisation numbers cannot drift from the run totals. Platform and
   signal counters remain source-specific; no universal "popularity" score is produced.
7. **Publishability → output**: only accepted records appear in `external.accepted_observation_ids`
   and in the footprint counters. Rejected records appear as `{id, reasons}` stubs only; their
   `source_url`, metrics and spans are not published. `external.publishable_only` is `true` and the
   contract validator enforces it.
8. **Diagnostics**: the run report carries `records_read`, `unique_records`, `accepted`, `rejected`,
   `unmatched`, `malformed`, `duplicates_collapsed`, `conflicting_duplicate_groups`,
   `ids_shared_across_organisations`, `content_duplicate_ids`, `rejection_reasons`,
   `per_organisation` and `connector_policy`.

## Run-to-run reuse and resume

External evidence is **never reused across runs**. `--resume` may reuse a completed official
profile from the profiles file, but any `external_footprint` block stored there by an earlier
invocation is dropped before processing, because it was validated against different inputs, at a
different time and possibly under a different rights/freshness policy. The external block is then
rebuilt only from the observations passed to the current invocation (none, if the flag is absent).

The run report records this policy in a `resume` block:
`profiles_reused`, `stale_external_blocks_discarded` and the policy string. `batch.py`'s contract
validator refuses any envelope whose `profile.external_footprint` has no matching top-level
`external` block, or whose accepted-observation ids do not match it, so a stale block cannot be
republished even if a future code path reintroduces one.

## Audit eligibility (authoritative policy, 2026-10-03)

Only *eligible* observations may take part in any audit number. Eligibility is decided in exactly one
place, `eligible_observations()` in `src/norway_company_agent/external_pipeline.py`, and
`audit_records()` calls it itself — the audit is therefore identical whether a caller hands over the
raw file rows or an already-gated list. A caller cannot re-open a closed gate by passing raw,
rejected, duplicated or out-of-batch records.

A record is eligible when **all** of the following hold:

1. **Deduplicated** — identity is `(organisation_number, id)`. Identical payloads for one identity
   collapse to the first occurrence (canonical fingerprints, so JSON key order is irrelevant).
2. **Not conflict-withheld** — payloads that disagree for one identity are withheld *entirely* and
   reported under `conflicting_groups`; the code never selects one payload as the winner.
3. **In scope** — when a batch is requested, the record's organisation number must be in it. A
   malformed identifier (not exactly nine ASCII digits, including zero-padded or integer forms) is a
   **data error** and is reported as ineligible, never silently treated as out of scope.
4. **Publication-policy accepted** — `validate_observation(..., organisation_number=…)` must report no
   reason: approved rights, approved (non-experimental) acquisition mode, verified exact entity,
   supported platform and signal type, evidence span where required, well-formed content hash.

**Stale is not ineligible.** Freshness is a *reporting scope* (`counts_scope`, `fresh_coverage`,
`stale_observation_ids`), not an eligibility rule: a stale but otherwise valid observation stays
eligible and auditable. This preserves the documented freshness semantics; only an explicit policy
decision may change it.

**Batch scope.** `organisation_numbers=None` means no batch restriction — the documented global
scope, every organisation is in scope. Passing an iterable (including an **empty** one) requests
batch-scoped qualification, so an empty batch qualifies nothing. The evaluator uses the profile
organisations as its batch (that is what `observations_out_of_batch` has always meant), and the
scorer uses the same organisation list.

Every audit report carries the decision, not just its result: `audit_records()["eligibility"]`
contains `batch_scoped`, `organisations_requested`, input/unique/eligible/ineligible/out-of-batch/
withheld counts, the conflicting groups, and a per-record `classifications` list where each input
record appears exactly once as `eligible`, `ineligible`, `out_of_batch` or `withheld_conflict`. The
evaluator publishes a compact projection of this under `audit_eligibility` plus the policy string in
`audit_eligibility_policy`.

Numbers this policy protects: `published_audited`, `audit_size`, `audit_size_gate`,
`qualification_passed`, `entity_precision`, `metric_precision`, `sentiment_accuracy` and the
per-organisation coverage breakdown all derive from the eligible set only, so repeating a row,
supplying a conflicting row, or attaching evidence for an organisation outside the batch cannot
inflate any of them. The 100-record gate consequently requires **100 distinct eligible observations**.

## Audit labels and the evaluator measurement

Audit labels are keyed by the composite identity `(organisation_number, id)` — the same identity the
gate uses — and `audit_records()` is the single place where the audit measurement is derived:

* identical duplicate labels collapse (reported under `labels_duplicate`);
* conflicting labels for the same composite identity are withheld entirely, reported under
  `labels_conflicting`, and make `qualification_passed` false — the code never picks one of two
  disagreeing human judgements;
* labels without a string `organisation_number`/`id` are reported as `labels_invalid`;
* labels with no eligible observation appear in `labels_without_observation`; in-scope observations
  with no label appear in `observations_without_labels`; `audit_coverage_by_organisation` breaks the
  coverage down per company;
* only eligible observations with a usable label count toward `published_audited` and
  `audit_size_gate`; a labelled identity that is in scope but did not qualify is reported under
  `audited_unpublished` instead of being silently dropped.

## Scorer trust boundary (`score_competition_v3.py`)

The proxy scores external measurements from the evaluator report. When the source artefacts are
supplied it re-derives them instead of trusting the report:

* `--observations PATH` → `coverage`, `fresh_coverage` and `connector_policy_passed` are computed by
  gating the observation file;
* `--observations PATH --labels PATH` → audit counts, precision figures, `audit_size_gate` and
  `qualification_passed` are computed by `audit_records()` over the gate's accepted records and the
  profile-derived batch. Audit eligibility (deduplication, conflict withholding, batch membership,
  publication policy) is enforced inside `audit_records()` by the shared selector, so this path and a
  raw-rows path produce the same measurement — asserted by
  `AuditEligibilityTests::test_c9_raw_and_gated_inputs_produce_the_same_audit`.

Any disagreement between the report and the derived values is listed in
`external_report_mismatches`, fails the `external_report_consistent` gate, and cannot qualify a
submission. Without the artefacts the scorer records `measurement_source: "unverified_report"` and
the external gates stay unproven unless the operator passes `--trust-external-report` (recorded as
`measurement_source: "trusted_report"`). No new scoring formula or threshold was introduced: the
rubric weights and gates are unchanged.

## Hash limitation (stated, not hidden)

Observation records do not carry the fetched bytes, so the gate **cannot recompute** the content
digest. It verifies the digest's syntax (64 lowercase hex characters) and detects identical payload
duplicates. Every run report records this under `hash_verification` with
`recomputed_from_bytes: false`. Recomputing digests requires the connector to emit the bytes (or a
signed capture reference); that is a connector-side change and is **not** implemented here.

## Rights, publication and connector policy

`acquisition_mode` and `rights_status` are record-level declarations by the connector:
`official_api`, `licensed_api`, `permitted_public_page` (and the other members of
`PUBLISHABLE_ACQUISITION_MODES`) publish; `*_experiment` modes never do. `connector_policy_passed`
means: at least one record is publishable, every published record declares an approved mode and
approved rights, and no published record used an experimental mode. It does **not** verify platform
terms — that remains a human/legal declaration, as recorded in the report's
`connector_policy.platform_terms_verified_here: false`.

## Reproducible commands (synthetic fixtures, no network)

```bash
# 1. Unit and contract tests
python -m pytest -q tests/test_poc.py

# 2. End-to-end verification of the competition CLI (temp workdir, ~2 s)
python scripts/verify_competition_e2e.py

# 3. One manual run over the synthetic batch
python scripts/run_competition_batch.py \
  --organisations tests/fixtures/batch-orgs-3.jsonl \
  --bulk tests/fixtures/bulk-registry-sample.csv.gz \
  --profiles-output /tmp/profiles.jsonl \
  --output /tmp/envelopes.jsonl \
  --report /tmp/report.json \
  --run-id demo-001 --expected-count 3 \
  --modules registry,accounting_obligation,website \
  --external-observations tests/fixtures/external-observations-valid.jsonl \
  --external-observations tests/fixtures/external-observations-adversarial.jsonl \
  --external-as-of 2026-08-24T00:00:00Z

# 4. Audit evaluator (produces fresh_coverage / connector_policy_passed)
python scripts/evaluate_external_footprint.py \
  --profiles /tmp/profiles.jsonl \
  --observations tests/fixtures/external-observations-valid.jsonl \
  --labels tests/fixtures/external-audit-labels.synthetic.jsonl \
  --as-of 2026-08-24T00:00:00Z \
  --output /tmp/ext-eval.json
```

## Provenance rules for external results

`tests/fixtures/README.md` labels every fixture. The rules for future published external results:

* a real connector run must record the exact command, the source, the retrieval time and the
  connector version; its digest must be computed over the bytes the connector fetched;
* sanitised or anonymised corpora must state what was removed and how identity was preserved;
* synthetic fixtures are mechanics tests only and can never be cited as coverage, accuracy or
  real-world validation;
* audit labels must come from an identified human or model reviewer with the labelling instruction
  recorded; a synthetic label file must never be counted toward the 100-record audit gate.

## Undocumented thresholds inherited from the baseline implementation

These values exist in `external_footprint.py` today. They are **not** specified in
`docs/external-footprint-loop.md` or in any other document in this repository, so they are recorded
here as implementation defaults rather than as agreed product policy. Changing them is a product
decision; this work preserved them rather than inventing new ones.

* `freshness_days = 45` (CLI: `--external-freshness-days`, default 45). The rubric only says
  freshness is worth 3 of 55 external points; the 45-day window is an implementation default.
* The sentiment gate: at least 10 accepted sentiment records **and** (at least 2 independent hosts
  **or** at least 10 independent reviewers). The reviewer test now counts distinct
  `(host, reviewer_id)` pairs instead of distinct reviewer ids, so the same reviewer on the same
  source counts once. Ten different reviewers on one platform still pass; ten copies of one reviewer
  no longer do.

## Contract-validator gaps recorded by the 2026-10-02 verification pass

Not fixed by the remediation sprint (out of its four-finding scope):

* a non-string `claims[].field` raises `TypeError` inside `validate_contract_envelope` instead of
  returning validation errors;
* external-block counters (`footprint.accepted_observations`, rejection counts) are not
  cross-checked against `accepted_observation_ids`/`rejected_observations`;
* a non-UTF-8 observation file raises `UnicodeDecodeError` instead of the operator-facing
  "Cannot read external observation file" error used for other read failures.

Their next actions are listed in the remediation sprint report's secondary-findings backlog.

## Known gaps (unchanged by this work)

* No connector in this repository produces observations at scale; the gate, the aggregator, the
  contract block and the verification runner are wired, but the connector fleet (Places, news RSS,
  jobs, YouTube, company-site extraction) still runs as separate scripts whose output has not been
  audited on a real corpus.
* `research.external_footprint_qa_passed` and `ux.external_intelligence_presented` in
  `scripts/score_competition_v3.py` still have **no producer**, so the research and UX categories
  remain capped. The proxy reports both as missing inputs and lists the limitation in
  `documented_limitations`.
