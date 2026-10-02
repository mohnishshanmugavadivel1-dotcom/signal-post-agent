# Integrity hardening report — S1–S9 (2026-10-03)

Mission: fix the remaining integrity defects S1–S9 listed in `docs/remediation-2026-10-02.md`
(section F), verify the fixes with the full mandatory battery, and report honestly what was and was
not executed. All work is in the working tree on branch `arena/01a0fd53-signal-post-agent`; it was
committed and pushed to that branch at the operator's request after the battery below ran (no merge,
no pull request, no deploy).

Environment: `/tmp/audit-venv/bin/python` = CPython 3.11.2 (GCC 12.2.0). `python3.12` and `uv` are
**absent**; the README's declared ≥ 3.12 floor is therefore **unverified**, and nothing in this
report claims 3.12 compatibility. No network was used for the verification runs. Probe sources and
logs are session-scoped under `/tmp/verify/` (`/tmp` was reset during the session, so the probes were
recreated; the recipes are in section F).

## A. Baseline and repository state

| Item | Value |
| --- | --- |
| Branch / base commit | `arena/01a0fd53-signal-post-agent` @ `c7175d85912ff6f8e9f3d24f8edafa3bbcc2a6cb` (main) |
| Working tree at mission start | 20 porcelain entries: 9 modified + 11 untracked; tracked diff +1,910/−158 |
| Working tree at final battery | tracked diff +2,413/−159 (mission-5 delta ≈ +503/−1 in tracked files, plus edits in the untracked `external_pipeline.py`) |
| Tree hash before battery | `bbf1d78bec05b865150546059cd6e50066ace71672738f2b5cdc42f985dd86e0` (all files except `.git`/`__pycache__`, sorted) |
| Tree hash after battery | identical (`bbf1d78b…`) → no source file changed during verification |
| Commits / pushes | the verified pre-commit working tree was committed and pushed to `arena/01a0fd53-signal-post-agent` at the operator's request after the battery; no merge, PR or deploy, and no human review yet |
| Report timing | this report was written **after** the battery; adding it changes the tree hash, so the hash above identifies the code state the battery ran against |

Known state inherited from mission 4 and re-confirmed here: F1–F4 fixed; S1–S9 open.
What changed in this mission: S1, S2, S3, S4, S5, S6, S7, S8, S9 are all fixed in the working tree,
S7 was the only one that still needed a scope decision (resolved: fixed with an allow-list), and the
regression suite grew from 136 to 153 tests (17 new tests in `IntegrityHardeningTests`).

## B. S1 — non-string `claims[].field` crashed the validator

**Defect.** `validate_contract_envelope` collected `claims[].field` and sorted it against the
`modules` keys, so a claim whose `field` was `None`, a dict, an int, or missing raised `TypeError`
out of a validator that is supposed to return structured errors.

**Reproduction (before).** `/tmp/verify/probes/s1_s2_repro.py` run against an isolated copy of the
tree with the field type-guard reverted (log `/tmp/verify/final/s1s2-before.log`):

```
[field = None] RAISED TypeError: '<' not supported between instances of 'str' and 'NoneType'
[field = {'a': 1}] RAISED TypeError: '<' not supported between instances of 'str' and 'dict'
[field = 7] RAISED TypeError: '<' not supported between instances of 'str' and 'int'
[field missing] RAISED TypeError: '<' not supported between instances of 'str' and 'NoneType'
[one malformed claim among valid] RAISED TypeError: '<' not supported between instances of 'int' and 'NoneType'
```

**Fix.** `src/norway_company_agent/batch.py`: the claim field is type-checked before it is used as a
display label, mapping key or sort key (`claim #N field must be a non-empty string`). The
claims-versus-modules comparison runs only when every claim carries a valid string field
(`claims_typed`), so one malformed claim can no longer cancel validation of the rest of the envelope
or crash the validator.

**After.** All five cases return `passed=False` with the structured error, e.g.
`errors=['claim #1 field must be a non-empty string']`; a valid envelope still passes
(`/tmp/verify/final/s1s2-after.log`, real CLI 3/3, 153 tests green).

**Tests.** `test_s1_malformed_claim_field_types_return_errors_instead_of_raising` (None, dict, int,
float, bool, empty string, whitespace, missing), `test_s1_one_malformed_claim_does_not_cancel_validation_of_the_rest`,
`test_s1_valid_claims_still_cover_the_requested_modules`, `test_s1_non_string_module_names_do_not_raise`.
Reverting the guard in the isolated copy makes the first test fail (fault injection, DETECTED).

## C. S2 — aggregate counts not cross-checked against the id lists

**Defect.** An envelope could publish `accepted_observation_ids: ["o1"]` together with
`footprint.accepted_observations: 999` (or duplicate ids, or an absent footprint, or the same id in
the accepted and rejected lists) and still validate.

**Reproduction (before).** Same probe, same isolated copy (log `s1s2-before.log`):

```
[consistent block] passed=True errors=[]
[count 999 vs 1 id] passed=True errors=[]
[duplicate accepted ids inflate the count] passed=True errors=[]
[counts absent] passed=True errors=[]
[same id accepted and rejected] passed=True errors=[]
[consistent empty block] passed=True errors=[]
```

**Fix.** New `_external_block_errors()` in `batch.py` performs the cross-checks the aggregation
policy in `external_footprint.aggregate_footprint` defines:

* `accepted_observation_ids` must be unique non-empty strings and
  `footprint.accepted_observations == len(ids)` (duplicates are refused, so repetition cannot inflate
  a count);
* `stale_observation_ids` unique, `footprint.stale_observations == len(stale ids)`, and stale ⊆ accepted;
* `footprint.rejected_observations == len(rejected_observations diagnostics)`; each diagnostic may
  carry only `id` + `reasons`, and a rejected id must be a non-empty string or null;
* `footprint.rejections` must be a list whose length matches the rejection diagnostics;
* `accepted_observations == fresh_observations + stale_observations`;
* `counts_scope ∈ {accepted, fresh}`, the block's `counts_scope` must match the footprint's, and
  `counted_observations` must equal `accepted` (or `fresh` for scope `fresh`);
* `status ∈ {available, not_available}` and `available` exactly when something was accepted;
* no id may appear in both the accepted and the rejected lists;
* a block without a `footprint` object is refused; a consistent **empty** block is still valid.

Producer coherence: `external_pipeline.company_external_block` now aggregates over the organisation's
whole gated record set (accepted + rejected), so `rejected_observations` matches the diagnostics the
block publishes; acceptance and freshness counters are unchanged. Without this, a real block showed
`rejected_observations: 0` next to N published rejections and the new check correctly failed it.

**After.** Every tampered case is refused with a specific reason, e.g.
`external footprint accepted_observations (999) does not match the accepted observation ids (1)`,
`external accepted_observation_ids must not repeat an identifier`,
`external block must carry a footprint object with its aggregate counts`,
`observations cannot be both accepted and rejected: ['o1']`; the consistent and empty blocks still
pass (`s1s2-after.log`; contract probe 14/14).

**Tests.** `test_s2_matching_counts_pass_and_mismatched_counts_are_rejected`,
`test_s2_duplicate_accepted_ids_cannot_inflate_a_count`,
`test_s2_aggregation_policy_invariants_are_enforced`,
`test_s2_missing_footprint_and_dangling_ids_are_rejected`,
`test_s2_empty_and_absent_external_evidence_stay_valid`,
`test_s2_real_gate_block_satisfies_every_cross_check`. Reverting the accepted-count check and the
duplicate-id guard in the isolated copy makes the corresponding tests fail (fault injection,
DETECTED).

**Residual gap (recorded, not fixed).** `footprint.platform_counts` is still only checked by the e2e
runner (`aggregation.footprint_matches_records`), not by the contract validator; a tampered
envelope that keeps `accepted_observations` consistent but lies in `platform_counts` would validate.
This was outside the S2 wording ("counts contradicting the accepted id list"); it is listed again in
section I.

## D. S3–S6 — contract-consistency fixes

| Id | Defect | Fix | Evidence |
| --- | --- | --- | --- |
| S3 | `claim.value` accepted while availability was `not_available` (`OUTPUT_CONTRACT.md`: value is null when unavailable) | `batch.py` refuses a non-null `value` when `availability != available` (`claim … must not carry a value when availability is not_available`) | probe S3 now `passed=False`; `test_s3_claims_without_data_must_not_carry_a_value`; injection DETECTED |
| S4 | non-UTF-8 observation file escaped as a raw `UnicodeDecodeError` traceback | `external_pipeline.read_observation_file` catches `UnicodeDecodeError` **before** `OSError` and raises `ObservationInputError("Cannot read external observation file <path>: not valid UTF-8 (…)")` | CLI probe: exit non-zero, no `Traceback`, file and reason named; suite test `test_s4_non_utf8_observation_file_raises_a_structured_error`; injection DETECTED |
| S5 | run timestamps only checked for being non-empty strings (`"yesterday"` passed) | `batch.py` validates `run.started_at`/`run.completed_at` with the shared ISO-8601 parser `external_footprint.parse_timestamp` (the same parser the freshness policy uses) | probe S5 now `passed=False` (`run.started_at must be an ISO-8601 timestamp`); `test_s5_run_timestamps_must_be_iso_8601`; injection DETECTED |
| S6 | envelope validator used `str.isdigit()` and accepted fullwidth digits (`１２３４５６７８９`), diverging from the gate's ASCII-only policy | `batch.py` imports the public `is_organisation_number` (exactly nine ASCII digits) from `external_footprint`; both surfaces now share one policy | probe S6 now `passed=False`; `test_s6_organisation_number_policy_is_shared_everywhere`; injection DETECTED; grep confirmed no test or runner asserted fullwidth acceptance |

## E. S7–S9 — decisions

* **S7 (unexpected `evidence[]` fields) — FIXED, allow-list.** `OUTPUT_CONTRACT.md` already documented
  `evidence[]` as "the documented fields only". The validator now refuses any key outside
  `CONTRACT_REQUIRED_EVIDENCE_FIELDS` (`evidence <id> carries undocumented fields: [...]`). An
  allow-list was chosen over a deny-list of suspicious names because a deny-list is bypassed by
  renaming (`internal_debug` → `debug_trace`); the check was verified against the union of evidence
  keys in the real generated envelopes (9 records, exactly the 6 documented fields). Blast radius: a
  future producer that adds a field must extend the documented list — fail-closed and visible.
  Doc line updated; `test_s7_undocumented_evidence_fields_are_rejected`; injection DETECTED.
* **S8 (NaN/Infinity under explicit report trust) — FIXED.** `scripts/score_competition_v3.py`
  `numeric()` rejects non-finite values with `math.isfinite` (booleans and non-numbers were already
  rejected), so NaN/±Infinity earn no credit. Reachable only when an operator passes
  `--trust-external-report`; the scorer probe shows Infinity/NaN coverage earning 0 where finite
  coverage earns the full weights (10/7/8/7 + freshness 3), and the freshness window is unchanged.
  `test_s8_non_finite_measurements_earn_no_credit`; injection DETECTED.
* **S9 (non-string ids in rejection diagnostics) — FIXED.** `external_footprint.diagnostic_id()`
  renders a non-string id as canonical JSON text (`["a", "b"]`), passes `None` and strings through,
  and is used by the gate stubs and the aggregate diagnostics; the validator refuses a non-string,
  non-null rejected id so a consumer can key diagnostics safely. `test_s9_rejection_diagnostics_carry_json_safe_ids`;
  injection DETECTED.
* **Deferred: none.** All nine defects are fixed; nothing in S1–S9 was left as a documented deferral.

## F. Verification

Commands run from `/home/user/signal-post-agent` with `PYTHONDONTWRITEBYTECODE=1
TLDEXTRACT_CACHE=/tmp/tldcache` and `/tmp/audit-venv/bin/python`. Battery driver:
`/tmp/verify/run_battery.sh`; logs `/tmp/verify/final/*.log`. The battery was run in full twice (the
second time after the fault-injection harness was extended from 12 to 17 injections); the table below
is the **final** run. Every step exited 0.

| # | Step | Exit | Result |
| --- | --- | --- | --- |
| 1 | `python -m pytest -q -p no:cacheprovider tests/` | 0 | 153 passed, 41 subtests passed |
| 2 | `python scripts/verify_competition_e2e.py` | 0 | checks: 52, failures: 0, critical: 0 |
| 3 | `python scripts/run_refresh_replay.py --manifest tests/fixtures/refresh-snapshots.json` | 0 | precision 1.0, recall 1.0, evidence_complete/idempotent_rerun/qualification_passed all true |
| 4 | official-only CLI (3 orgs, no external flags) | 0 | 3/3 envelopes pass, 0 external blocks, no external section |
| 5 | external CLI (valid + adversarial fixtures) | 0 | 21 read / 18 unique / 7 accepted / 12 rejected / 1 unmatched / 2 malformed; fresh 6, stale 1; fresh_coverage 0.666667; connector policy true; contract 3/3 |
| 6 | `resume_probe.py` (F1) | 0 | 12 checks, 0 failed |
| 7 | `crash_repro.py` (F2) | 0 | both scenarios pass: contract 3/3, malformed rejected, valid record still accepted |
| 8 | `gate_probe.py` (F2) | 0 | 17 cases, 0 failed |
| 9 | `fuzz_accepted.py` (F2) | 0 | 21 cases, 0 crashes, 0 accepted-despite-malformed |
| 10 | `fuzz_probe.py` (F2) | 0 | 99 cases, 0 crashes, 0 accepted-despite-malformed, 0 gate disagreements |
| 11 | `eval_probe.py` | 0 | 9 checks, 0 failed |
| 12 | `scorer_probe.py` (F4/S8) | 0 | 12 checks, 0 failed |
| 13 | `contract_probe.py` (S1–S9) | 0 | 14 cases: 14 safeguards, **0 defects** (was 5 safeguards / 9 defects at mission 4) |
| 14 | `cli_probe.py` (S4) | 0 | 7 checks, 0 failed (non-UTF-8 now a clean structured error) |
| 15 | `fault_injection.py` (isolated copy) | 0 | 17 injections, 17 detected, 0 undetected, 5/5 files restored and hash-verified |

Tree hash and `git diff` hash were identical before and after the battery, so no source file changed
during verification and no re-run was required on that account. One `__pycache__` directory created
by interactive probe runs was removed afterwards (excluded from the tree hash).

**Before/after reproduction logs:** `/tmp/verify/final/s1s2-before.log` (pre-fix state re-enacted in
`/tmp/verify/iso` by reverting the S1 guard and the external cross-checks) and `s1s2-after.log`
(working tree). Neither the working tree nor the file-integrity checks are affected by the isolated
copy.

**What was not executed.** No live registry/connector/platform/network result; no Python 3.12 run; no
`uv`/lockfile install; no independent reviewer run; no official competition evaluation or rubric; no
100-record real-world audit. `data.brreg.no`/`builderr.ai` are unreachable from the sandbox.

## G. F1–F4 regression re-confirmation

* F1 (stale external evidence on resume): `resume_probe` 12/12 — a second `--resume` run publishes no
  external blocks and drops the two stored ones (`profiles_reused 3`,
  `stale_external_blocks_discarded 2`), and rebuilding from current input works.
* F2 (malformed record crashed the batch): `crash_repro` both scenarios pass and `gate_probe` 17/17,
  `fuzz_accepted` 21/21, `fuzz_probe` 99 cases with no crash.
* F3 (labels not bound to the composite identity): `eval_probe` shows a label for another
  organisation becomes an orphan with `published_audited` dropping by exactly one, and the evaluator's
  identity policy is the `(organisation_number, id)` composite.
* F4 (fabricated measurements could close gates): `scorer_probe` shows derived-vs-reported
  field-level mismatches, `external_report_consistent` failing, `awardable_score 0`, and the
  explicit `trusted_report` / default `unverified_report` boundary.
* Fault injection re-detects all of them: F1 (2), F2 (2), F3 (1), F4 (2) of the 17 injections.

## H. Final diff inspection

Files changed by this mission:

| File | Mission-5 change |
| --- | --- |
| `src/norway_company_agent/batch.py` | S1 typed claim field + `claims_typed`; S2 `_external_block_errors` + `counts_consistent` check; S3 value guard; S5 shared ISO parser; S6 shared ASCII organisation-number policy; S7 evidence allow-list |
| `src/norway_company_agent/external_footprint.py` | S6 public `is_organisation_number`; S9 `diagnostic_id()` (+ JSON-safe diagnostics) |
| `src/norway_company_agent/external_pipeline.py` (untracked file) | S4 `UnicodeDecodeError` → `ObservationInputError`; S9 `diagnostic_id` in gate stubs and per-org block; S2 producer aggregates over the org's gated record set |
| `scripts/score_competition_v3.py` | S8 `math.isfinite` in `numeric()` (+ `math` import) |
| `tests/test_poc.py` | `IntegrityHardeningTests`: 17 tests / 19 subtests for S1–S9 |
| `OUTPUT_CONTRACT.md` | documents that the evidence allow-list is enforced |

Inspection results: changes are confined to the S1–S9 scope; no unrelated refactoring, no stylistic
rewrite, no debug code, no machine-specific paths, no new dependency, no deleted or weakened test, no
new escape hatch. Every behavioural change is additive (new refusals) except the S2 producer change,
which makes the published rejection counts match the diagnostics already emitted. The tracked diff is
+2,413/−159 overall; the mission-5 delta is ≈ +503/−1 in tracked files. `external_pipeline.py` is
untracked, so `git diff` cannot measure its share — the changes there are listed explicitly above and
reviewed by reading the file.

## I. Remaining risks and environment limitations

1. **Self-verification only.** Every result above was produced by the implementing agent. No
   independent verification of the post-fix state has been performed, and none is claimed.
2. **`platform_counts` is not validated in the contract path** (only in the e2e runner). A tampered
   envelope can misreport per-platform counts while remaining count-consistent; recorded as a
   residual S2-adjacent gap, not fixed in this mission.
3. **`contract_version` value is not validated.** The validator requires the field but accepts any
   string (observed while building the contract probe; `OUTPUT_CONTRACT.md` states the produced value
   `signalpost-minimal-v1`). Outside the S1–S9 scope; consumers that care must compare it themselves.
4. **Environment is not the declared one.** CPython 3.11.2, no `uv`, no lockfile-driven install; the
   ≥ 3.12 floor is unverified. `python3.12`/`uv` absence is a sandbox limitation, not a project
   statement.
5. **Synthetic data only.** No real connector corpus or live source was exercised; the 100-record
   audit gate is unmet by the fixtures and remains unmet. No accuracy, calibration, competitiveness
   or real-world validity claim is made or supported. The external CLI numbers refer to the bundled
   synthetic fixtures.
6. **Scorer producers still missing** (`research.external_footprint_qa_passed`,
   `ux.external_intelligence_presented`), so those categories stay capped — unchanged by this
   mission.
7. **Evidence durability.** Probes and logs live in `/tmp/verify/` and the sandbox reset `/tmp`
   during this session; the probe sources would have to be rewritten (or recovered from this report's
   recipes) after a reset. The tree hash and diff hash make re-verification deterministic.
8. **Review state.** All S1–S9 fixes, tests and docs sit in one commit together with the earlier
   F1–F4 and implementation work, pushed to the session branch at the operator's request; a human
   reviewer should still inspect the diff before treating it as reviewed. Nothing was merged,
   deployed, or opened as a pull request.

## J. Readiness

* **Code integrity (S1–S9): REMEDIATED AND SELF-VERIFIED.** All nine defects listed in
  `docs/remediation-2026-10-02.md` section F are fixed in the working tree, each has a regression
  test that fails when the fix is reverted in an isolated copy, and the full 15-step battery
  (153 tests, 52 e2e checks, probes, 17/17 fault injections) passed with an unchanged tree hash.
* **Independent verification: NOT PERFORMED.** The verification above is a self-run; an independent
  reviewer should re-run section F before release. This is not independent QA.
* **Real-world product readiness: NOT ESTABLISHED.** The audit gate is unmet on synthetic data, no
  real connector corpus or official competition evidence exists, the declared Python floor is
  unverified, and the residual gaps in section I remain. No score, accuracy or readiness threshold is
  claimed.
