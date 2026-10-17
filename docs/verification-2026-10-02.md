# Verification record — 2026-10-02

Evidence for the post-audit implementation work on branch `arena/01a0fd53-signal-post-agent`
(commit `c7175d85912ff6f8e9f3d24f8edafa3bbcc2a6cb`, changes left uncommitted in the working tree).

Every number below was produced by the command shown, in this workspace, on 2026-10-02. Nothing
here is a projection or an estimate. Claims that cannot be made from this evidence are listed under
"Limits of this record".

## Environment

| Item | Value |
| --- | --- |
| Interpreter | CPython 3.11.2 (`/tmp/audit-venv/bin/python`) |
| Declared floor | `pyproject.toml` requires ≥ 3.12 — this run is below it; the suite passes, but the version claim is not verified |
| Dependencies | installed only in `/tmp/audit-venv` (`bs4`, `extruct`, `tldextract`, `trafilatura`, `pypdf`, `pytest`); nothing installed into the repository or the base environment |
| Network | not used by any command in this record; `TLDEXTRACT_CACHE=/tmp/tldcache`, `PYTHONDONTWRITEBYTECODE=1` |
| Data | synthetic fixtures only (`tests/fixtures/README.md`) |
| Working-tree hash (all files excluding `.git`) | `a989774a1c3417be0883cbf5fce78e3824f192f839ca5ff060fc2c6c9c77c798` |
| Pre-change reference hash | `990eb4c265871f6ddbf3f126cb2a64a8f17fdcf6393e0cad3d8156045c697da9` |

## Results

### 1. Focused unit and contract tests

```
python -m pytest -q -p no:cacheprovider tests/test_poc.py
```

Observed: `124 passed, 5 subtests passed in 0.89s`, exit 0. Baseline before this work: `104 passed,
5 subtests passed in 0.86s`, exit 0. The 20 new tests cover entity binding, hash syntax, duplicate
collapse/conflict, shared ids across organisations, rights rejection, freshness separation,
malformed JSONL, determinism, and the output-contract invariants.

### 2. End-to-end verification runner

```
python scripts/verify_competition_e2e.py --python /tmp/audit-venv/bin/python --json-report /tmp/audit-out/verify/e2e-checks.json
```

Observed: `checks: 44 | failures: 0 | critical: 0`, exit 0. The runner executes the real
competition CLI twice (determinism), the official-only path, the external evaluator, the refresh
replay, the proxy scorer and the prototype builder, all against synthetic fixtures in a temporary
work directory. It includes two controlled negative controls (a tampered envelope in memory and an
empty observation file); both controls were detected.

### 3. Refresh replay (official path, unchanged)

```
python scripts/run_refresh_replay.py --manifest tests/fixtures/refresh-snapshots.json --output /tmp/audit-out/verify/refresh.json
```

Observed: `precision 1.0, recall 1.0, evidence_complete True, idempotent_rerun True,
qualification_passed True`, exit 0. This result is unchanged from the baseline and refers only to
the bundled synthetic snapshot fixture.

### 4. Competition CLI with external observations

```
python scripts/run_competition_batch.py \
  --organisations tests/fixtures/batch-orgs-3.jsonl \
  --bulk tests/fixtures/bulk-registry-sample.csv.gz \
  --profiles-output /tmp/audit-out/verify/profiles.jsonl \
  --output /tmp/audit-out/verify/envelopes.jsonl \
  --report /tmp/audit-out/verify/report.json \
  --run-id verify-001 --expected-count 3 --modules registry,accounting_obligation,website \
  --external-observations tests/fixtures/external-observations-valid.jsonl \
  --external-observations tests/fixtures/external-observations-adversarial.jsonl \
  --external-as-of 2026-08-24T00:00:00Z
```

Observed, exit 0:

| Field | Value |
| --- | --- |
| `records_read` / `unique_records` | 21 / 18 |
| `accepted` / `rejected` / `unmatched` / `malformed` | 7 / 11 / 2 / 2 |
| `duplicates_collapsed` / `conflicting_duplicate_groups` | 1 / 1 |
| `fresh_observations` / `stale_observations` | 6 / 1 |
| `fresh_coverage` / `publishable_coverage` | 0.666667 / 0.666667 |
| `connector_policy_passed` | true |
| contract validation | 3 envelopes validated, 3 passed, `external_block_present: 2` |
| `validate_envelopes` | passed, zero silent drops |

The same command with `--enforce-freshness` reports `accepted 7`, `fresh 6`, `stale 1`,
`counts_scope "fresh"` — acceptance is unchanged, counting scope changes.

### 5. Scorer and evaluator

Observed within the e2e runner: the evaluator produces `fresh_coverage = 0.6666666666666666` and
`connector_policy_passed = true` for the fixture corpus, reports the two malformed lines, and
refuses to qualify a 3-record synthetic label file (`audit_size_gate false`,
`qualification_passed false`). The proxy scorer runs, declares `research` and `ux` as missing
inputs, records both limitations, and exits non-zero when `--require-complete-inputs` is passed.
Observed proxy output for this fixture set: `raw_score 12.0`, `awardable_score 0`,
`qualification_passed false`, failing gates `external_audit_at_least_100` and
`official_identity_complete`.

## Limits of this record

* No live registry, connector, platform or network result was produced or verified here. External
  observations come from synthetic fixtures.
* The digest gate verifies syntax and content duplicates only; `recomputed_from_bytes` is `false`
  because observation records do not carry fetched bytes.
* The 100-record independent audit gate is **not** satisfied by any artifact in this repository, and
  the synthetic label file is explicitly not an audit.
* `research.external_footprint_qa_passed` and `ux.external_intelligence_presented` still have no
  producer, so those scorer categories remain capped.
* The work was implemented and verified by the same agent; independent QA (Stage 7) has **not** been
  performed. The negative controls in the e2e runner are self-checks, not independent review.
* Nothing here demonstrates product accuracy, calibration, or competitiveness.
