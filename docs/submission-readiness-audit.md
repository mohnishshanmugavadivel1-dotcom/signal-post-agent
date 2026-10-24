# Signalpost — Submission Readiness & Release Verification Audit

**Audit Date**: October 18, 2026  
**Repository**: `https://github.com/mohnishshanmugavadivel1-dotcom/signal-post-agent`  
**Current PR**: [PR #2: Fix company data integrity and publication gates](https://github.com/mohnishshanmugavadivel1-dotcom/signal-post-agent/pull/2)  
**Base**: `origin/main` (`5776d6a`) | **Head**: `fix/d1-d5-data-integrity-hardening` (`662fffb`)  
**CI Workflow**: `.github/workflows/ci.yml` (merged via PR #3, verified passing on Python 3.12 and 3.13)

---

## 1. Executive Summary

Signalpost is an evidence-backed intelligence agent for researching Norwegian companies, anchoring entity identity in the Brønnøysund registry, fetching official annual accounts and registered roles, auditing company websites, and admitting external observations through strict entity-rights and freshness gates.

Following an independent forensic investigation and subsequent repair sprints:
1. Defects D1–D5 (status parsing, cross-entity contamination, employee count boundary validation, financial comparison boundaries, and website quarantine leaks) have been remediated and sealed.
2. Automated GitHub Actions CI has been added and verified passing across both Python 3.12 and 3.13.
3. The offline competition verification suite (`verify_competition_e2e.py`) passed all 54 pipeline integrity checks.
4. The deterministic refresh replay (`run_refresh_replay.py`) passed with 100% precision and recall (2/2 expected changes, 0 false positives).
5. PR #2 is fully tested, free of conflicts, and ready for human maintainer review.

---

## 2. Challenge Submission Contract Checklist

Evaluated against the official Builderr challenge specification documented in `README.md` ("Submission contract"):

| Contract Item | Requirement | Current Evidence | Verification Method | Status | Action Needed |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Smoke Test Result** | A 100-company smoke-test result or report | Synthetic 100-company verifier passes (`test_verifier_accepts_a_real_run`). `artifacts/README.md` documents policy: real run requires live network access to `data.brreg.no`. | `scripts/verify_smoke_artifacts.py` | **BLOCKED (Network)** | Execute live smoke run on a machine with unrestricted HTTPS access to `data.brreg.no`. |
| **2. Documented CLI Command** | One documented command that accepts a JSONL batch of org numbers | `python scripts/run_competition_batch.py --organisations <in.jsonl> --bulk <brreg.csv> --profiles-output <out.jsonl> --output <envelopes.jsonl> --report <report.json>` | Documented in `README.md` and verified in `verify_competition_e2e.py` | **PASS** | None. Fully verified. |
| **3. Terminal Envelopes** | Exactly one terminal envelope per input organization | E2E pipeline and test suite verify exact 1-to-1 input-to-envelope mapping with zero silent drops. | `verify_competition_e2e.py` (step 1 & 4) | **PASS** | None. Verified. |
| **4. Pinned Dependencies** | Pinned dependencies and reproducible setup | `pyproject.toml` (`>=3.12`) and committed lockfile `uv.lock`. Tested in GitHub Actions on Python 3.12 & 3.13. | `uv sync --frozen` in GitHub Actions CI | **PASS** | None. Fully verified. |
| **5. Material Change Output** | Previous-snapshot input and material-change output | `scripts/run_refresh_replay.py` reproduces 2 true positive changes on saved snapshots. | `run_refresh_replay.py` (1.0 precision, 1.0 recall) | **PASS** | None. Fully verified. |
| **6. Machine-Readable Report** | Run report with runtime, request count, and third-party cost | `run-report.json` with `operations` block (total requests, p50/p95 latency, complete `third_party_cost_usd` aggregation). | `tests/test_poc.py::RunReportCostTests` | **PASS** | None. Fully verified. |
| **7. Declared Models & Rights** | Declared models, APIs, licences, and source-rights assumptions | `README.md`, `OUTPUT_CONTRACT.md`, and `docs/norway-sources.md` declare NLOD 2.0 open registry data, unmetered official APIs, and rights gates. | Code inspection & `OUTPUT_CONTRACT.md` | **PASS** | None. Documented. |

---

## 3. System Architecture & End-to-End Data Flow

```
[Input: batch-orgs.jsonl]
          │
          ▼
┌────────────────────────────────────────────────────────┐
│ 1. Bulk Registry Anchor (sampling.py)                  │
│    - Loads local Brønnøysund snapshot (gzip CSV)       │
│    - Normalizes status: active vs adverse vs unknown   │
│    - Validates employee count (non-negative int)       │
└────────────────────────────────────────────────────────┘
          │
          ▼
┌────────────────────────────────────────────────────────┐
│ 2. Live Official Modules (official.py)                 │
│    - Fetches data.brreg.no APIs (financials, roles)   │
│    - Verifies 9-digit matching organization identity   │
│    - Rejects cross-entity responses as source_error    │
└────────────────────────────────────────────────────────┘
          │
          ▼
┌────────────────────────────────────────────────────────┐
│ 3. Website Audit & Identity Gate (batch.py, research.py)│
│    - Crawls registry-listed website                    │
│    - Computes identity assessment                      │
│    - Quarantines unverified sites (availability=none)  │
└────────────────────────────────────────────────────────┘
          │
          ▼
┌────────────────────────────────────────────────────────┐
│ 4. External Evidence Pipeline (external_pipeline.py)   │
│    - Deduplicates identical observations               │
│    - Withholds conflicting observations                │
│    - Enforces batch membership and rights policies     │
└────────────────────────────────────────────────────────┘
          │
          ▼
┌────────────────────────────────────────────────────────┐
│ 5. Envelope & Claims Builder (batch.py)                │
│    - Emits 1 terminal JSONL envelope per input         │
│    - Cites SHA-256 evidence records                    │
│    - Validates contract schema before serialization    │
└────────────────────────────────────────────────────────┘
          │
          ▼
┌────────────────────────────────────────────────────────┐
│ 6. Refresh & Verification (refresh.py, verify_smoke)   │
│    - Computes deterministic field diffs                │
│    - Verifies run report cost, counts, and signatures  │
└────────────────────────────────────────────────────────┘
```

---

## 4. Test Evidence Summary

- **Local Python 3.14.2 (Windows)**:
  - `pytest tests/test_d1_d5_regression.py -q`: **35 passed in 0.12s**
  - `pytest tests/ -q`: **225 passed, 57 subtests passed in 10.71s**
- **Remote GitHub Actions CI (Linux)**:
  - **Python 3.12**: 35 regression passed (0.09s), 225 full suite passed (6.96s)
  - **Python 3.13**: 35 regression passed (0.08s), 225 full suite passed (4.61s)
- **Pipeline Mechanics & Determinism**:
  - `verify_competition_e2e.py`: **54 checks passed, 0 failures**
  - `run_refresh_replay.py`: **1.0 Precision, 1.0 Recall, qualification_passed: true**

---

## 5. Known Limitations & Remaining Risks

1. **Live Smoke Run (`artifacts/smoke-001`)**:
   - The repository currently lacks a published live run report under `artifacts/smoke-001/report.json`. Producing this requires live network access to `data.brreg.no` on an unfirewalled machine. Synthetic fixtures prove mechanics but must not be substituted for live data.
2. **D4 Scope Boundary**:
   - `validate_revenue_comparison()` is a standalone validation utility. The production pipeline performs equality diffing on profile fields and single-year financial fact retrieval; it does not calculate numerical CAGR or growth rates at runtime.
3. **External Scorer Baseline**:
   - The external-first baseline score on the proxy evaluator is currently ~34/100 without live external connectors (Google Places, YouTube, LinkedIn). Unlocking higher competitive scores requires ingesting audited external observations that pass the blind audit gate.

---

## 6. Reproducible Execution & Evaluator Demo Script

### Step 1: Run Full Test Suite
```bash
uv sync --frozen
uv run pytest tests/ -v
```
*Expected: 225 tests and 57 subtests pass with 0 failures.*

### Step 2: Run Targeted D1–D5 Regression Suite
```bash
uv run pytest tests/test_d1_d5_regression.py -v
```
*Expected: 35 tests pass covering D1 through D5.*

### Step 3: Run Deterministic Refresh Replay Demo
```bash
uv run python scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```
*Expected: Identifies exactly 2 material changes, 0 false positives, precision 1.0, recall 1.0.*

### Step 4: Run End-to-End Pipeline Verification
```bash
uv run python scripts/verify_competition_e2e.py
```
*Expected: 54/54 checks pass across contract, acceptance, rejection, isolation, and determinism.*
