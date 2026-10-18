# Signalpost — D1–D5 Remediation and Data Integrity Report

**Repository**: `mohnishshanmugavadivel1-dotcom/signal-post-agent`  
**Base Commit**: `5776d6a2a0ef88f4b00aa64049d97a08b5346e63`  
**PR #2 Branch**: `fix/d1-d5-data-integrity-hardening`  
**PR #2 URL**: [https://github.com/mohnishshanmugavadivel1-dotcom/signal-post-agent/pull/2](https://github.com/mohnishshanmugavadivel1-dotcom/signal-post-agent/pull/2)  
**Status**: All 35 targeted regression tests pass. Full test suite (225 tests, 57 subtests) passes across Python 3.12, 3.13, and 3.14.

---

## 1. Executive Summary

During an independent forensic audit of the Signalpost company intelligence pipeline, five critical data-integrity and publication defects (D1–D5) were identified and reproduced. These defects permitted deregistered entities to be sampled as active, allowed mismatched organization registry responses to contaminate company profiles, accepted negative and malformed employee counts into research claims, left financial comparison boundaries unspecified, and allowed quarantined website data to be emitted as high-confidence claims.

All five defects have been remediated with minimal, isolated modifications across four source modules and validated with a new 35-test regression suite.

---

## 2. Detailed Technical Remediation

### D1: Deregistered / Dissolved Companies Classified as Active
- **Module**: `src/norway_company_agent/sampling.py`
- **Root Cause**: `stratum()` and `financial_filer_eligible()` evaluated company activity solely by checking `bankrupt is False and liquidating is False`. Furthermore, `normalize_row()` discarded deletion dates (`slettedato`), deletion flags, and forced dissolution flags (`underTvangsavvikling`). Missing, null, or empty fields defaulted to inactive adverse flags, treating unknown records as active.
- **Fix**:
  1. Implemented `parse_status_bool()` supporting documented Norwegian and English boolean encodings (`"1"`, `"ja"`, `"j"`, `"true"`, `"yes"` as truthy; `"0"`, `"nei"`, `"n"`, `"false"`, `"no"` as falsy; all others as `None`).
  2. Updated `normalize_row()` to capture `slettedato`, `deleted`, and `forced_dissolution`.
  3. Hardened `stratum()` and `financial_filer_eligible()`: Active status requires verified falsity across `bankrupt`, `liquidating`, `deleted`, and `forced_dissolution`. If any adverse flag is truthy or `slettedato` is present, the status is classified as `adverse`. If any flag is missing, malformed, or contradictory, the status evaluates to `unknown` and is excluded from active-only sampling frames.
- **Tests**: `TestD1CompanyStatus` and `TestD1StatusHardening` (10 tests in `test_d1_d5_regression.py`).

### D2: Cross-Entity Registry Contamination
- **Module**: `src/norway_company_agent/official.py`
- **Root Cause**: `fetch_official_modules()` accepted `registry_live` HTTP responses from Brønnøysund without verifying that the returned 9-digit organization number matched the requested company. A mismatched or proxy response could silently overwrite company name, legal form, and registered status.
- **Fix**:
  1. Added explicit organization number extraction and normalization (`payload_org = (entity_data.get("organisasjonsnummer") or "").replace(" ", "")`).
  2. Enforced strict 9-digit matching against the requested organization.
  3. On mismatch, missing organization, or malformed data, sets `status = "source_error"` and appends diagnostic notes (e.g. `f"registry payload org {payload_org} does not match requested {expected_org}"`).
- **Tests**: `TestD2OrganisationIdentity` (5 tests in `test_d1_d5_regression.py`).

### D3: Negative and Malformed Employee Counts
- **Modules**: `src/norway_company_agent/sampling.py`, `official.py`, `research.py`
- **Root Cause**: Negative numbers (such as `-5`), string representations (`"-5"`, `"five"`), and non-integer floats were either unvalidated or passed through directly into stratum calculations and research profile answers.
- **Fix**:
  1. Implemented `parse_employee_count(val)`: Strictly rejects booleans (`True`/`False`), floats (`3.5`), negative values (`-1`, `"-5"`), and non-numeric strings. Returns integer `>= 0` or `None`.
  2. Integrated `parse_employee_count()` across bulk registry row normalization, live entity parsing, and the response boundary in `answer_profile()`.
- **Tests**: `TestD3EmployeeCounts` and `TestD3EmployeeHardening` (9 tests in `test_d1_d5_regression.py`).

### D4: Financial Comparison Scope and Reporting Period Boundary
- **Module**: `src/norway_company_agent/official.py`
- **Root Cause**: The repository lacked validation ensuring that multi-period financial comparisons compared identical corporate scopes (company vs group), matching currencies, and equivalent 12-month reporting periods.
- **Fix**:
  1. Added `validate_revenue_comparison(prior, current) -> tuple[bool, str | None]` to `official.py`.
  2. Enforces matching scope (`prior["scope"] == current["scope"]`), matching currency, exactly 12-month duration, non-empty periods, and chronological ordering (`prior["period_end"] <= current["period_start"]`).
  3. Returns `(True, None)` or `(False, rejection_reason)`.
- **Architectural Clarification**: Signalpost v1 (`signalpost-minimal-v1`) does **not** compute numerical revenue growth, delta percentages, or multi-year revenue change claims in runtime execution. Runtime change detection in `refresh.py` performs field-level equality diffs (`diff_profile`) across `TRACKED_FIELDS`, and `research.py` queries single-year facts. `validate_revenue_comparison()` is delivered as a **standalone, unit-tested validation utility** in `official.py` and is deliberately not connected to runtime execution to preserve evaluator output contract compliance.
- **Tests**: `TestD4RevenueComparison` (6 tests in `test_d1_d5_regression.py`).

### D5: Website Quarantine Publication Gate
- **Modules**: `src/norway_company_agent/batch.py`, `research.py`
- **Root Cause**: In `batch.py`, website evidence whose identity assessment failed verification (`publishable = False`) could still be emitted in `claims` as `availability: "available"` with high confidence (`0.99`), exposing unverified domains as verified company websites.
- **Fix**:
  1. Enforced strict boolean validation in `build_claims_and_evidence()` and `build_errors()`: `identity_assessment.get("publishable") is True`.
  2. When unverified or quarantined, sets `availability = "not_available"`, `confidence = 0.0`, and suppresses the value (`value = None`).
  3. Legitimate error states (`blocked`, `failed`) are preserved.
  4. In `research.py`, `answer_profile()` suppresses descriptions from unpublishable websites.
- **Tests**: `TestD5WebsiteQuarantine` (5 tests in `test_d1_d5_regression.py`).

---

## 3. Test Matrix and Verification

| Environment | Suite | Tests | Result | Duration |
| :--- | :--- | :--- | :--- | :--- |
| **Local (Windows, Py 3.14.2)** | Targeted D1–D5 | 35 | **35 Passed** | 0.12s |
| **Local (Windows, Py 3.14.2)** | Full Suite | 225 (+ 57 subtests) | **225 Passed** | 10.71s |
| **GitHub Actions (Linux, Py 3.12)** | Targeted D1–D5 | 35 | **35 Passed** | 0.09s |
| **GitHub Actions (Linux, Py 3.12)** | Full Suite | 225 (+ 57 subtests) | **225 Passed** | 6.96s |
| **GitHub Actions (Linux, Py 3.13)** | Targeted D1–D5 | 35 | **35 Passed** | 0.08s |
| **GitHub Actions (Linux, Py 3.13)** | Full Suite | 225 (+ 57 subtests) | **225 Passed** | 4.61s |
| **E2E Pipeline Script** | `verify_competition_e2e.py` | 54 checks | **54 Passed** | 5.82s |
| **Refresh Replay Script** | `run_refresh_replay.py` | 2 events, 1.0 P/R | **Passed** | 0.45s |
