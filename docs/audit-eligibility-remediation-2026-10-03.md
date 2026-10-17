# Audit-eligibility remediation — Findings A, B, C (2026-10-03)

Sprint report for the independently reproduced audit-integrity defects:

* **Finding A** — duplicate and conflicting observations inflated audit size.
* **Finding B** — out-of-batch observations inflated audit size.
* **Finding C** — the scorer passed raw observations into audit processing, bypassing
  deduplication, conflict withholding and batch-membership decisions.

Code state: branch `arena/01a0fd53-signal-post-agent`, base commit under audit
`c0d557ec27252a190e453b452c4ff6d85c4492be`. Environment: `/tmp/audit-venv/bin/python` = CPython
3.11.2 (Python 3.12 and `uv` are absent, so the declared ≥ 3.12 floor remains unverified); no network
was used. All results below are **self-verification by the implementing agent**, not independent
verification.

## 1. Root cause

`audit_records()` in `src/norway_company_agent/external_pipeline.py` derived the audit measurement by
walking **the list it was handed**:

```python
for item in observations:                       # raw rows, as given
    if publishable_observation(item):           # structural-only predicate
        published.append(...)                   # one row, one audited record
```

Three consequences, one per finding:

* **A** — nothing deduplicated the rows and nothing withheld conflicting payloads, so *n* copies of one
  record produced *n* audited records. `observation_fingerprint` and `deduplicate_observations`
  existed, but the audit never called them.
* **B** — the `organisation_numbers` argument only fed the per-organisation coverage breakdown; no
  record was filtered by batch membership, so evidence for organisations outside the requested batch
  counted toward that batch's audit gate.
* **C** — `publishable_observation(item)` was called **without** `organisation_number`, which is the
  structural-only mode of `validate_observation` (the organisation-binding check is skipped). Any
  caller handing over raw or partially filtered rows therefore re-opened a gate that had already
  rejected or withheld them: rejected, unmatched, duplicated and conflicting rows all audited
  normally.

Measured before the fix (probe `--expect` log, `/tmp/verify/final/audit-before.log`): one record
repeated 100× → `published_audited 100`, gate **true**, qualification **true**; 100 records belonging
only to another organisation → `100 / true / true`; two conflicting payloads for one identity →
`2 / true / true`; and the evaluator CLI reproduced the same numbers (`published_audited=100`,
`qualification_passed=True` for a single duplicated row with a 100-record minimum).

## 2. Fix design

**One authoritative eligibility decision.** New `eligible_observations(observations, *,
organisation_numbers=None)` in `external_pipeline.py` classifies every input record exactly once:

| Status | Rule |
| --- | --- |
| `withheld_conflict` | payloads disagree for one `(organisation_number, id)`; the whole group is withheld (never one arbitrary winner) |
| `ineligible` | fails the publication policy, or the organisation number is not nine ASCII digits (a data error, never "outside batch") |
| `out_of_batch` | well-formed record for an organisation outside the requested batch |
| `eligible` | deduplicated, in scope, policy-accepted — and only these may be audited |

`organisation_numbers=None` preserves the documented **global** scope; an iterable (including an empty
one) requests **batch-scoped** qualification, so an empty batch qualifies nothing. Freshness is *not*
an eligibility rule — stale records stay eligible — which preserves the existing documented semantics.

**The audit enforces it itself.** `audit_records()` calls the selector on whatever it is given, so the
result is identical for raw rows and for an already-gated list. That is what closes Finding C without
relying on callers to pre-filter; the contract is enforced by the function, not by convention.

**Consumers share the decision.** The evaluator derives every external metric (`published_audited`,
precision, `platform_counts`, `acquisition_modes`, fresh/stale counts, coverage) from the eligible set,
and reports a compact `audit_eligibility` block plus `audit_eligibility_policy` so the decision is
visible in the artefact. The scorer passes the gate's accepted records *and* the profile-derived batch
to `audit_records()`, and its artifact-derived measurement still overrides report values, so a
fabricated "100-record audit" claim now mismatches the derivation, fails
`external_report_consistent`, and cannot qualify.

No scoring formula, threshold, rubric weight or gate name changed; no existing test was modified or
removed.

## 3. Verification

Commands ran from `/home/user/signal-post-agent` with
`PYTHONDONTWRITEBYTECODE=1 TLDEXTRACT_CACHE=/tmp/tldcache` and `/tmp/audit-venv/bin/python` (3.11.2).
Logs: `/tmp/verify/final/`. Battery driver: `/tmp/verify/run_battery.sh`.

| # | Step | Exit | Result |
| --- | --- | --- | --- |
| 1 | `python -m pytest -q -p no:cacheprovider tests/` | 0 | **181 passed, 44 subtests passed** (was 153/41) |
| 2 | same, `-k "RemediationRegressionTests or IntegrityHardeningTests"` | 0 | 29 passed, 36 subtests — F1–F4 and S1–S9 stay pinned |
| 3 | `python scripts/verify_competition_e2e.py` | 0 | checks: 52, failures: 0, critical: 0 |
| 4 | `python /tmp/verify/probes/audit_eligibility_probe.py` | 0 | **29 cases, 29 matching expectations, 0 differing** (before: 18 differing) |
| 5 | `python /tmp/verify/probes/fault_injection.py` (isolated copy) | 0 | **14 injections, 14 detected**, 5/5 files restored and hash-verified |

Tree hash before and after the battery are identical
(`2b3f042ade9aab111cd54953a8b99b6f49debcec7005f7f1837e0e987e7e299a`) and the tracked-diff hash is
unchanged, so nothing mutated during verification.

### Adversarial cases (probe, `/tmp/verify/final/b4-probe.log`)

Must **not** qualify — all now refused:

| Case | Before | After |
| --- | --- | --- |
| one observation repeated 100× | 100 / gate ✓ | **1** / gate ✗ |
| 99 copies + 1 distinct | 100 / gate ✓ | **2** / gate ✗ |
| conflicting payloads, one identity | 2 / gate ✓ | **0** / gate ✗ |
| 100 records all outside the batch | 100 / gate ✓ | **0** / gate ✗ |
| empty batch | 100 / gate ✓ | **0** / gate ✗ |
| mixed in-batch / out-of-batch | 100 / gate ✓ | **50** / gate ✗ |
| invalid or zero-padded identifiers | 0 (already) | **0**, reported as data errors, not batch misses |

Must **qualify** — the control case still does:

| Case | Before | After |
| --- | --- | --- |
| 100 distinct, eligible, in-batch, labelled | 100 / gate ✓ | **100** / gate ✓ |
| stale but otherwise valid records | 100 / gate ✓ | **100** / gate ✓ (freshness semantics unchanged) |
| evaluator CLI E3 control | — | `published_audited=100`, `qualification_passed=True` |

Scorer CLI after remediation: duplicate input derives **1** (a report claiming 2 mismatches;
`external_report_consistent` false); a fabricated 100-record claim mismatches, fails
`external_audit_at_least_100` and `external_report_consistent`, `awardable_score 0`; out-of-batch
input derives **0**; the honest report on 100 distinct records is consistent, passes the audit gate and
records `measurement_source: derived_from_artifacts`.

### New tests

`tests/test_poc.py`: `AuditEligibilityTests` (23 tests — A1–A7, B1–B8, C1–C10) and
`AuditCliIntegrityTests` (5 tests — evaluator and scorer CLI entry points). Fault injection confirms
each new guard is test-pinned: disabling the batch filter, the conflict withholding, the classifier,
the publication-policy check, the malformed-identifier branch, the evaluator's eligible-set derivation
or the scorer's consistency comparison each makes its targeted test fail.

## 4. Documentation

`docs/external-observations.md` gains an **Audit eligibility (authoritative policy)** section covering
the four eligibility rules, the batch-scope semantics (`None` = global, empty = nothing), the
stale-versus-ineligible distinction, the protected metrics, and the per-record classification block
that every audit report now carries. The scorer section records that the audit enforces eligibility
itself and that the raw and gated paths are proven equivalent.

## 5. Remaining limitations and unverified assumptions

1. **Self-verification only.** No independent reviewer has retested this state; the independent
   reviewer must confirm before the fix is treated as verified.
2. **Synthetic fixtures only.** No real connector corpus, no live source, no official competition
   evaluation. The 100-record audit gate is still unmet by any artefact in this repository, and no
   accuracy or readiness claim is made.
3. **`organisation_numbers=None` is the documented global scope**, but no CLI entry point currently
   exercises a genuinely unscoped production run: the evaluator and scorer always derive a batch from
   the profiles. The unscoped path is unit-tested (`test_b4`) and used by library callers.
4. **Conflict diagnostics keep their existing shape.** `build_audit_index` reports a third label for an
   already-conflicted identity as a duplicate rather than a second conflict; the identity is withheld
   either way. This pre-existing diagnostic detail was deliberately not changed; the tests assert the
   effect (identity unusable, count reduced) rather than the breakdown.
5. **Evaluator batch semantics are now explicit.** The evaluator's batch is its profile set, so
   evidence for organisations absent from the profiles no longer counts toward its audit. This is the
   Finding B fix, and it changes evaluator numbers for corpora that mix in-batch and out-of-batch rows.
6. **Scratch artefacts are session-scoped** (`/tmp/verify/*`) and `/tmp` has been reset repeatedly in
   this environment; the probe and battery sources would need recreating from this report after a
   reset. The tree and diff hashes above make re-verification deterministic.
7. **Python 3.12 compatibility remains unverified** (3.11.2 runtime, no `uv`, no lockfile install).
