# Remediation sprint report — 2026-10-02 (A–J)

Scope: the four priority findings from the independent verification of 2026-10-02 (F1 Critical,
F2 High, F3 High, F4 High). No new features, no redesign, no commits. All changes remain in the
working tree on branch `arena/01a0fd53-signal-post-agent` for human review.

This file is the durable copy of the A–J report delivered in the session. It is documentation only:
no code or test changed after the verification battery except the addition of this file.

## A. Baseline

| Item | Value |
| --- | --- |
| Branch | `arena/01a0fd53-signal-post-agent` |
| HEAD | `c7175d85912ff6f8e9f3d24f8edafa3bbcc2a6cb` (remote has the same single commit; verified earlier via `gh api`) |
| Working tree before the sprint | 9 modified + 10 untracked, +1,279/−79, tree hash `e661664e80445a1f2bd084410d1efea3d3b381451708903e5e06662a6da193ed` |
| Working tree after the sprint | 9 modified + 10 untracked, +1,910/−158, tree hash `2e121d52e7d265943d71889f1b521bd742b30ac11dce8f5977ff69d35b7bed14` (battery state) |
| Interpreter | CPython 3.11.2 only. `python3.12` and `uv` are **absent** (`command -v` re-checked at the start of this verification); the declared floor in `pyproject.toml` is ≥ 3.12, so version compatibility is **not established** |
| Dependencies | re-installed into a fresh `/tmp/audit-venv` after the sandbox reset `/tmp` between sessions (pytest 9.1.1, bs4, extruct, tldextract, trafilatura, pypdf). Nothing was installed into the repository or the base environment |
| Data | synthetic fixtures only (`tests/fixtures/README.md`) |
| Known pre-existing defect list | F1–F10 from the independent verification; F5–F10 are carried in section F |

Freeze/inspection performed before testing: `git rev-parse` (branch/HEAD), `git status --porcelain=v1`
(19 entries, listed in section H), full-tree SHA-256, `git diff --stat`, `git diff` for every
modified file, and a scan of every changed/added file for `TODO|FIXME|breakpoint()|/home/user|
/tmp/|C:\\|localhost` (hits only in a pre-existing SSRF-negative test and in documentation command
examples). No `__pycache__`, no `.pyc`, no stray `out/` directory.

## B. F1 — Resume fix

*Defect (Critical): a `--resume` run without `--external-observations` republished the previous
invocation's `profile.external_footprint` even though the run report said `external_block_present: 0`.*

- Reproduction before the fix (recorded in the earlier session): resume run exit 0, report without
  an `external` section; the profiles file and envelopes still carried 2 stale external blocks; the
  contract validator passed.
- Root cause: resume reused prior profile dicts wholesale; only the top-level `external` block was
  rebuilt per invocation.
- Fix: `scripts/run_competition_batch.py` drops any stored `external_footprint` when the profile is
  reused (`discard_stale_external`) and reports it in a `resume` block
  (`profiles_reused`, `stale_external_blocks_discarded`, policy string);
  `batch.py::validate_contract_envelope` refuses any envelope whose `profile.external_footprint`
  has no matching top-level `external` block, or whose accepted ids differ from it.
- Policy (explicit): external evidence is **never reused across runs**; it is rebuilt only from the
  current invocation's gated observations. Documented in `docs/external-observations.md`.
- Verification: `resume_probe.py` 12/12 checks, exit 0 — no `external` key in report or envelopes,
  `stale_external_blocks_discarded: 2`, 0 blocks in the profiles file, official claims identical
  across run 1 and run 3, block rebuilt when observations are supplied again. Fault injection
  removed the stale-drop (`DETECTED`) and the validator guard (`DETECTED`).

## C. F2 — Input typing

*Defect (High): malformed nested values aborted the whole batch — e.g. `metrics: "..."` raised
AttributeError before any output was written; unhashable `sentiment_label`/`signal_type` values
raised TypeError in membership tests; malformed organisation numbers were mislabelled `unmatched`.*

- Reproduction before the fix (recorded in the earlier session): `crash_repro` exit 1, no outputs,
  traceback `run_competition_batch.py:175 → external_pipeline.py:276 → external_footprint.py:218-219`.
- Fix: `external_footprint.validate_observation` is fully type-guarded (string ids, 9 ASCII-digit
  organisation number, string+membership for `platform`/`signal_type`/`acquisition_mode`,
  `signal_type_ok` pre-computed before any membership use, string `evidence_span`, `metrics` object
  with non-negative finite `likes`/`comments`/`shares`, typed sentiment triple, list-of-objects
  `identity_proof`); `metrics: None` remains "absent", not malformed; no silent coercion
  (`exact_entity: "true"` is rejected). `external_pipeline` classifies malformed organisation
  numbers as data errors (`missing or invalid organisation number`) instead of `unmatched`.
- Verification: `crash_repro.py` exit 0 with outputs written — malformed record rejected with
  `metrics must be an object`, valid neighbour accepted, contract 3/3; an unhashable list id also
  exits 0 with a rejection diagnostic and contract 3/3. `gate_probe.py` 17/17 cases.
  `fuzz_accepted.py` 21 cases, 0 crashes, 0 accepted-despite-malformed. `fuzz_probe.py` 99 cases,
  0 crashes, 0 accepted-despite-malformed, 0 rejected-without-reason. Fault injection removed the
  organisation-number check (`DETECTED`) and the metrics check (`DETECTED`). Contract-level
  decision preserved: malformed *lines* are reported and never abort; an unreadable *file* is an
  operator error and stops the run.

## D. F3 — Label identity

*Defect (High): audit labels were keyed by `str(id)` alone, so one label audited three published
records from three different organisations (`published_audited 3`, `entity_precision 1.0`).*

- Fix: shared policy `AUDIT_IDENTITY_POLICY = "organisation_number + id"` and helpers
  `audit_identity` / `build_audit_index` / `audit_records` / `coverage_from_observations` in
  `external_pipeline.py`; `evaluate_external_footprint.py` derives every label and coverage number
  from `audit_records`. Identical duplicate labels collapse and are reported; conflicting labels for
  one composite identity are withheld, reported, and force qualification off (never silently
  resolved); labels without a string organisation number/id are `labels_invalid`; labels with no
  observation and observations with no label are listed; coverage is broken down per organisation.
- Verification: `eval_probe.py` 10/10 checks — legacy un-scoped label audits nothing and is
  diagnosed; three composite labels audit exactly one record each; per-organisation coverage
  denominators are 1/1/1; duplicate label collapses; conflicting label withheld; unlabelled records
  are excluded from the denominator and listed. Fault injection reduced the key to `id` only
  (`DETECTED`).

## E. F4 — Scorer trust boundary

*Defect (High): the proxy read audit counts from the evaluator report verbatim, so a fully
fabricated "perfect" report produced `raw_score 75.0` with `missing_inputs: []`.*

- Fix: `score_competition_v3.py` derives measurements from the source artefacts when supplied
  (`--observations`, optionally `--labels`), compares them with the report
  (`external_report_mismatches`), fails the new `external_report_consistent` gate on any
  disagreement, and records `measurement_source`
  (`derived_from_artifacts` / `trusted_report` / `unverified_report`) plus a `measurement_trust`
  block. Without artefacts the external gates stay unproven unless the operator passes
  `--trust-external-report`; the trust decision is recorded. Report-only runs no longer award
  external or coverage points. **No scoring formula, rubric weight or product threshold was
  changed**; `--minimum-audit` remains 100 and `--target` remains 80.
- Verification: `scorer_probe.py` 12/12 checks — honest and fabricated report-only runs both give
  `awardable_score 0`; the trust flag is explicit and recorded; a mismatching report is rejected
  (`coverage`, `fresh_coverage`, `audit_size_gate`, `published_audited`) with
  `awardable_score 0`; artefact-matched runs derive cleanly with no mismatch and the unaudited
  corpus still cannot qualify; non-finite coverage cannot earn credit. Fault injection re-enabling
  implicit trust in the gates, in the measurement flag, and in the flag itself were all `DETECTED`.

## F. Secondary findings

Evidence: `contract_probe.py` (14 cases: 5 SAFEGUARD, 9 DEFECT) and the earlier verification record.

| # | Finding | Severity | File / function | Reproduction evidence | Potential impact | Blocks F1–F4? | Next action |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | Non-string `claims[].field` raises `TypeError` in the validator | High | `src/norway_company_agent/batch.py::validate_contract_envelope` | `claims[0]["field"] = None/{"a":1}/7` → `TypeError: '<' not supported between 'str' and …` | A corrupt/tampered envelope crashes a consumer instead of failing validation; inconsistent with "validate before write" | No | Type-guard claim/evidence field keys before sorting; return a validation error; add a regression test |
| S2 | External counters not cross-checked against ids | High | `src/norway_company_agent/batch.py` validator (producer: `external_footprint.aggregate_footprint`) | `accepted_observation_ids: ["o1"]` + `footprint.accepted_observations: 999` → `passed=True` | Published counts can contradict the published id list; a consumer trusting counts is misled | No (the scorer path is covered by F4's derivation; the envelope path is not) | Cross-check id-list length and rejection counts against the footprint before validating; regression test |
| S3 | `claim.value` accepted while the module is `not_found` | Medium | `src/norway_company_agent/batch.py` validator | `financials` availability `not_available` with `value: {"profit": 1}` → `passed=True`; `OUTPUT_CONTRACT.md` says value is null when unavailable | A value without availability support can be published and read as a claim | No | Reject non-null `value` when availability is not `available` (or require evidence support); regression test |
| S4 | Non-UTF-8 observation file raises `UnicodeDecodeError` | Medium | `src/norway_company_agent/external_pipeline.py::read_observation_file` | CLI with byte `0xc3 0x28` → exit 1, traceback, no report | Operator-facing failure is a raw traceback; no structured diagnostic | No | Catch `UnicodeDecodeError`, raise `ObservationInputError` with the file path like other read errors |
| S5 | Non-ISO run timestamps accepted | Medium | `src/norway_company_agent/batch.py` validator | `run.started_at = "yesterday"` → `passed=True` | A consumer parsing timestamps fails downstream; contract says ISO-8601 | No | Validate with the shared timestamp parser and report an error |
| S6 | Envelope organisation number accepts fullwidth digits | Low | `src/norway_company_agent/batch.py` (check uses `isdigit`), vs `external_footprint._is_organisation_number` (ASCII) | `organisation_number = "１２３４５６７８９"` → `passed=True`; zero-padded and int forms are rejected | Contract and observation gate disagree on what a valid organisation number is (gate is stricter, so the direction is conservative) | No | Reuse the ASCII nine-digit check in the contract validator |
| S7 | Unexpected `evidence[]` fields accepted | Low | `src/norway_company_agent/batch.py` validator | `evidence[0]["internal_debug"] = {...}` → `passed=True` | Leniency could let internal data reach a published envelope (external data is already blocked) | No | Decide allow-list vs deny-list; at minimum reject internal/external-looking keys |
| S8 | NaN/Infinity still earn full credit under explicit report trust | Low | `scripts/score_competition_v3.py::capped/numeric` | trusted report with `coverage: Infinity` → full coverage points | Only reachable with the explicit, recorded trust flag | No | Make `numeric()` reject non-finite values |
| S9 | Rejection diagnostics preserve a non-string id verbatim | Low | `external_pipeline`/`batch.py` rejected-entry schema | list id → `{"id": ["a","b"], "reasons": [...]}` | A consumer keying rejections by id can crash (as the probe's own dict did) | No | Emit a safe string form for non-string ids in diagnostics |

Safeguards confirmed (no action): rejected entries in envelopes are restricted by the validator to
`id` + `reasons` (an entry carrying `source_url` is refused), and the real gate output leaks no
source URL into envelopes or the run report's external section.

## G. Verification

All commands were run from `/home/user/signal-post-agent` with
`PYTHONDONTWRITEBYTECODE=1 TLDEXTRACT_CACHE=/tmp/tldcache` and
`/tmp/audit-venv/bin/python` (CPython 3.11.2). Logs: `/tmp/verify/final/*.log` (session-scoped).
Probe sources: `/tmp/verify/probes/` (session-scoped; recreated this session after the sandbox
reset — the earlier session's copies were lost with `/tmp`).

| Step | Command | Exit | Result | Skipped |
| --- | --- | --- | --- | --- |
| 1 | `python -m pytest -q -p no:cacheprovider tests/` | 0 | 136 passed, 22 subtests passed | none |
| 2 | `python scripts/verify_competition_e2e.py --json-report …` | 0 | 52 checks pass, 0 failures, 0 critical | none |
| 3 | `python scripts/run_refresh_replay.py --manifest tests/fixtures/refresh-snapshots.json --output …` | 0 | precision 1.0, recall 1.0, evidence_complete/idempotent/qualification all true | none |
| 4 | official-only CLI (3 orgs, no external flags) | 0 | 3 envelopes, 0 external blocks, `validation.passed=True`, contract 3/3 | none |
| 5 | external CLI (valid + adversarial fixtures) | 0 | 21 read / 7 accepted / 12 rejected / 1 unmatched / 2 malformed; fresh_coverage 0.667; connector policy true; 2 external blocks; contract 3/3 | none |
| 6 | `python resume_probe.py` | 0 | 12 checks, 0 failed | none |
| 7 | `python crash_repro.py` | 0 | both scenarios pass (string-metrics and list-id), contract 3/3 | none |
| 8 | `python gate_probe.py` | 0 | 17 cases, 0 failed | none |
| 9 | `python fuzz_accepted.py` | 0 | 21 cases, 0 crashes, 0 accepted-despite-malformed | none |
| 10 | `python fuzz_probe.py` | 0 | 99 cases, 0 crashes, 0 accepted-despite-malformed, 0 rejected-without-reason | none |
| 11 | `python eval_probe.py` | 0 | 10 checks, 0 failed | none |
| 12 | `python scorer_probe.py` | 0 | 12 checks, 0 failed | none |
| 13 | `python contract_probe.py` | 0 | 14 cases recorded (9 open defects listed in F) | none |
| 14 | `python fault_injection.py` (negative control, isolated copy) | 0 | control 12 passed; 8 injected faults, 8 detected, 8 files restored (hash-verified) | none |

Warnings: none in any log. Environment limitations: Python 3.12/`uv` absent (declared floor not
verified); dependency versions come from a freshly built scratch venv, not from a locked file;
probes and logs live in `/tmp` and are session-scoped; no network was used.

Self-verification vs independent verification: **all of the above is self-verification** performed
by the agent that implemented the fixes. The earlier independent verification was a separate
mission with its own read-only rules; it covered the pre-fix state. No independent verification of
the post-fix state has been performed, and none is claimed.

## H. Diff summary

Working tree at the battery hash `2e121d52…`: 9 modified + 10 untracked files, +1,910/−158
(previous implementation state: +1,279/−79).

| File | Kind | Content of the sprint change |
| --- | --- | --- |
| `scripts/run_competition_batch.py` | modified | F1 stale-block drop + `resume` report block (atop the earlier external-gate wiring) |
| `src/norway_company_agent/batch.py` | modified | F1 validator guard for stored external blocks (atop the contract envelope work) |
| `src/norway_company_agent/external_footprint.py` | modified | F2 type guards, metric fields, defensive aggregation |
| `src/norway_company_agent/external_pipeline.py` | added | intake/gate pipeline; F3 `audit_records` + shared coverage |
| `scripts/evaluate_external_footprint.py` | modified | F3 composite labels, diagnostics, no arbitrary label selection |
| `scripts/score_competition_v3.py` | modified | F4 derivation, consistency gate, explicit trust boundary |
| `tests/test_poc.py` | modified | F1–F4 regression tests (12 new tests) |
| `scripts/verify_competition_e2e.py` | added | e2e runner, now 52 checks incl. `typing.*` and `resume.*` |
| `docs/external-observations.md` | added | schema, pipeline, reuse policy, label identity, scorer trust boundary, recorded gaps |
| `docs/verification-2026-10-02.md`, `OUTPUT_CONTRACT.md`, `README.md`, `scripts/build_prototype.py`, `tests/fixtures/*` | added/modified | earlier implementation mission's documentation, contract status table, prototype gate wiring and synthetic fixtures |

Inspection results: no unrelated refactoring; no safeguard weakened (the one new escape hatch
`--trust-external-report` is opt-in, off by default, and recorded in the output — the default
direction is stricter than before); no hardcoded machine paths in code (only `ROOT`-relative
resolution); no debug code or temporary artifacts; no `__pycache__`; documentation matches observed
behaviour (spot-checked: `OUTPUT_CONTRACT.md`'s confidence-presence claim against real envelopes,
`docs/external-observations.md`'s reuse/label/trust claims against the probes); the F1–F4 tests
reproduce the defects — proven by fault injection, which detected all 8 re-introduced defects,
including the three F4 variants.

One change was made during verification: two assertions were added to the F4 regression test after
fault injection showed the original test did not pin the trust boundary (it passed with implicit
trust). The affected tests were re-run (suite 136 passed) and the fault-injection run repeated
(8/8 detected). The battery above was then run in full against the updated tree.

## I. Remaining risks

1. **No independent verification of the post-fix state.** Everything in section G was produced by
   the implementing agent. An independent reviewer should re-run section G before any release.
2. **Environment not the declared one.** Python 3.11.2, no `uv`, no lockfile-driven install. The
   declared ≥ 3.12 floor is unverified; nothing here shows 3.12 compatibility.
3. **Secondary defects remain open** (S1–S9). S1 (validator crash on malformed claim fields) and S2
   (counts not cross-checked against ids) are integrity-relevant: S2 means the scorer path is now
   protected by derivation, but the *envelope* path can still publish counts that contradict the
   published ids. None of these blocks F1–F4; all should be fixed before external consumers rely on
   published counts.
4. **Trust flag semantics.** `--trust-external-report` lets report-only measurements satisfy gates.
   It is explicit and recorded, but an operator who passes it can still be misled by a fabricated
   report; the mitigation is detection (or omission) of the flag, not cryptography.
5. **Synthetic data only.** No real connector corpus was audited; connectors still produce no
   publishable observations under current rights defaults (F8 from the verification report). The
   audit gate (100 labelled records) cannot be satisfied by the fixtures and remains unmet.
6. **Scorer/UX producers missing** (`research.external_footprint_qa_passed`,
   `ux.external_intelligence_presented`), so those categories stay capped — unchanged by this
   sprint.
7. **Evidence durability.** Battery logs and probes live in `/tmp` (session-scoped). If the sandbox
   is reset, the commands in section G reproduce them; the probe sources would need rewriting (they
   are described case-by-case in sections B–F).

## J. Readiness

**FOUR PRIORITY DEFECTS FIXED AND VERIFIED**

Conditions for that statement: all four fixes are present in the frozen tree, each original
reproduction now passes, all 14 battery steps executed green (including the e2e runner's 52 checks
and 8/8 fault-injection detections), and no priority defect remains unverified. This is
**self-verification**, not independent verification, and it says nothing about product-level
readiness: the secondary defects in section F remain open, the declared Python floor is unverified,
the audit gate is unmet on synthetic data, and no accuracy, calibration, competitiveness or
real-world validity claim is made or supported.

Changes remain uncommitted in the working tree for human review.
