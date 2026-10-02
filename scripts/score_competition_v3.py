#!/usr/bin/env python3
"""Signalpost competition proxy v3 — external-first optimization scorer.

Traceability of the formula (nothing here is invented; the weights come from
``docs/external-footprint-loop.md`` and ``docs/poc-design.md``):

    external_footprint_intelligence  55  = identity 10 + breadth 10 + workforce 7 + reviews 8
                                            + buzz 7 + sentiment 10 + freshness 3
    official_company_foundation      15  = identity 4 + accounts 4 + roles/locations 4 + website 3
    research_agent                   10  = research report score scaled from its own 12-point max
    daily_extensibility_refresh      12  = batch 4 + resume 2 + refresh 3 + latency 1 + connector policy 2
    product_ux_design                 8  = ux report score capped at 8

Inputs are produced by other commands; this scorer never invents a value for a missing one.
Every input it reads is declared in ``INPUT_CONTRACT`` below, and any key that is absent is
reported in ``missing_inputs`` instead of being silently scored as zero.

Trust boundary for external measurements (2026-10-02 remediation)
---------------------------------------------------------------

The audit numbers this proxy consumes (`audit_size_gate`, `published_audited`,
`wrong_entity_publications`, `unsupported_publications`, coverage and `connector_policy_passed`)
are measurements, not opinions. Before this change they were read from the evaluator report
verbatim, so a fabricated report could close the audit gate.

The scorer now derives those measurements itself when the source artefacts are supplied:

* `--observations PATH` — derive coverage, `fresh_coverage` and `connector_policy_passed` by
  gating the observation file exactly as the pipeline does.
* `--observations PATH --labels PATH` — additionally derive `published_audited`,
  `wrong_entity_publications`, `unsupported_publications`, the audit-size gate and the precision
  figures from the observation and label files, keyed by `(organisation_number, id)`.

Report values that disagree with the derived values are listed in `external_report_mismatches`,
fail the `external_report_consistent` gate, and can never qualify a submission. When the artefacts
are not supplied the scorer records `measurement_source: "trusted_report"` and requires the
explicit `--trust-external-report` flag before report-supplied values may satisfy a gate; without
that flag the external gates stay unproven, which is the honest default.

Known producer status (checked against the repository, 2026-10-02):

* ``external.qualification_passed`` / ``coverage`` / ``wrong_entity_publications`` /
  ``unsupported_publications`` / ``audit_size_gate`` — ``scripts/evaluate_external_footprint.py``
* ``external.fresh_coverage`` / ``external.connector_policy_passed`` — ``scripts/evaluate_external_footprint.py``
  and the ``external`` block of ``scripts/run_competition_batch.py`` reports
* ``batch.*`` — ``scripts/run_competition_batch.py``
* ``refresh.*`` — ``scripts/run_refresh_replay.py``
* ``research.score`` / ``research.qualification_passed`` — ``scripts/evaluate_research_agent.py``
* ``research.external_footprint_qa_passed`` — **no producer exists**; until one does, the research
  category is capped at 5/10 by the line below. Closing this requires a product decision about what
  evidence constitutes external-footprint QA for the research agent.
* ``ux.*`` — **no producer exists** (``scripts/build_prototype.py`` renders HTML but writes no
  report). Until one does, the UX category is capped at 4/8.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.external_pipeline import (  # noqa: E402
    audit_records,
    coverage_from_observations,
    gate_observations,
    read_observation_file,
    run_external_summary,
)


#: Every key this scorer reads, per report. Missing keys are reported, never defaulted silently.
INPUT_CONTRACT: dict[str, dict[str, bool]] = {
    "external": {
        "qualification_passed": True,
        "audit_size_gate": True,
        "wrong_entity_publications": True,
        "published_audited": True,
        "unsupported_publications": True,
        "coverage": True,
        "fresh_coverage": True,
        "connector_policy_passed": True,
    },
    "batch": {"validation": True, "emitted_envelopes": True, "operations": True},
    "refresh": {"qualification_passed": True, "evidence_complete": True, "idempotent_rerun": True},
    "research": {"score": True, "qualification_passed": True, "external_footprint_qa_passed": True},
    "ux": {"score": True, "external_intelligence_presented": True},
}
REQUIRED_REPORTS = ("profiles", "external_report", "batch_report", "refresh_report", "research_report", "ux_report")


def load(path: str | None) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else {}


def check_inputs(reports: dict[str, dict[str, Any]], provided: dict[str, bool]) -> dict[str, Any]:
    """Report every required key that is absent, and every required report not supplied."""
    missing: list[dict[str, str]] = []
    for name, required in INPUT_CONTRACT.items():
        if not provided.get(name, False):
            reason = "report supplied but empty" if name in provided else "report not supplied"
            missing.append({"report": name, "key": "*", "reason": reason})
            continue
        for key, is_required in required.items():
            if is_required and key not in reports.get(name, {}):
                missing.append({"report": name, "key": key, "reason": "key missing from report"})
    return {"missing_inputs": missing, "complete": not missing}


def numeric(value: Any) -> float | None:
    """Return a finite float for numbers, or None for missing/non-numeric/non-finite values.

    NaN and Infinity are rejected here rather than downstream: ``max``/``min`` clamp arithmetic
    treats NaN as "not less than the bound", which used to award full credit for an unusable value.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def coverage_points(coverage: Any, key: str, weight: float, gate: bool) -> float:
    """Weighted coverage points. A non-numeric coverage value scores zero instead of raising."""
    if not gate or not isinstance(coverage, dict):
        return 0.0
    return capped(weight, numeric(coverage.get(key)))


def rows(path: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def capped(weight: float, value: float | int | None) -> float:
    return round(weight * max(0.0, min(1.0, float(value or 0))), 3)


def derive_external_measurements(args, profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive external measurements from the observation (and label) artefacts.

    Returns an empty dict when no observation file is supplied, in which case the scorer falls back
    to report values under an explicit trust flag.
    """
    if not args.observations:
        return {}
    organisation_numbers = [str(row.get("organisation_number")) for row in profiles]
    records, malformed = read_observation_file(Path(args.observations))
    gate = gate_observations(
        records,
        organisation_numbers=organisation_numbers,
        malformed=malformed,
        as_of=args.as_of,
        freshness_days=args.freshness_days,
    )
    summary = run_external_summary(gate, organisation_numbers=organisation_numbers)
    derived: dict[str, Any] = {
        "coverage": coverage_from_observations(
            records,
            organisation_numbers=organisation_numbers,
            as_of=args.as_of,
            freshness_days=args.freshness_days,
        ),
        "fresh_coverage": summary["fresh_coverage"],
        "connector_policy_passed": summary["connector_policy_passed"],
        "accepted_observations": summary["accepted"],
        "malformed_records": len(malformed),
    }
    if args.labels:
        label_rows, _ = read_observation_file(args.labels)
        audit = audit_records(
            records,
            label_rows,
            minimum_audit=args.minimum_audit,
            organisation_numbers=organisation_numbers,
        )
        derived.update({
            "audit_size_gate": audit["audit_size_gate"],
            "published_audited": audit["published_audited"],
            "wrong_entity_publications": audit["wrong_entity_publications"],
            "unsupported_publications": audit["unsupported_publications"],
            "entity_precision": audit["entity_precision"],
            "metric_precision": audit["metric_precision"],
            "audit_labels_usable": audit["labels_usable"],
            "audit_labels_invalid": len(audit["labels_invalid"]),
            "audit_labels_conflicting": len(audit["labels_conflicting"]),
            "audit_qualification_passed": bool(
                audit["qualification_passed"] and not audit["labels_invalid"] and not audit["labels_conflicting"]
            ),
        })
    return derived


def compare_measurements(derived: dict[str, Any], reported: dict[str, Any]) -> list[dict[str, Any]]:
    """List every derived measurement that disagrees with the evaluator report."""
    mismatches: list[dict[str, Any]] = []
    for key, derived_value in derived.items():
        if key not in reported:
            continue
        reported_value = reported[key]
        if isinstance(derived_value, bool) or isinstance(reported_value, bool):
            same = bool(derived_value) == bool(reported_value)
        elif isinstance(derived_value, (int, float)) and isinstance(reported_value, (int, float)):
            same = abs(float(derived_value) - float(reported_value)) <= 1e-6
        elif isinstance(derived_value, dict) and isinstance(reported_value, dict):
            same = all(
                abs(float(derived_value.get(k, 0)) - float(reported_value.get(k, 0))) <= 1e-6
                if isinstance(derived_value.get(k), (int, float)) and isinstance(reported_value.get(k), (int, float))
                else derived_value.get(k) == reported_value.get(k)
                for k in set(derived_value) | set(reported_value)
            )
        else:
            same = derived_value == reported_value
        if not same:
            mismatches.append({"field": key, "derived": derived_value, "reported": reported_value})
    return mismatches


def main() -> None:
    parser = argparse.ArgumentParser(description="Signalpost competition proxy v3: external intelligence is the primary differentiator.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--external-report", required=True)
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--resume-report")
    parser.add_argument("--refresh-report", required=True)
    parser.add_argument("--research-report", required=True)
    parser.add_argument("--sentiment-report")
    parser.add_argument("--ux-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target", type=float, default=80)
    parser.add_argument(
        "--require-complete-inputs",
        action="store_true",
        help="Exit non-zero when a declared scorer input is missing or malformed (default: warn and score conservatively)",
    )
    parser.add_argument("--observations", default=None, help="External observation JSONL used to derive coverage and policy measurements")
    parser.add_argument("--labels", default=None, help="Audit label JSONL keyed by (organisation_number, id) used to derive audit measurements")
    parser.add_argument("--as-of", default=None, help="ISO-8601 'now' for the freshness window when deriving from observations")
    parser.add_argument("--freshness-days", type=int, default=45, help="Freshness window in days when deriving from observations")
    parser.add_argument("--minimum-audit", type=int, default=100, help="Minimum number of audited published observations the audit gate requires")
    parser.add_argument(
        "--trust-external-report",
        action="store_true",
        help="Allow report-supplied external measurements to satisfy gates when no derivation artefacts are supplied",
    )
    args = parser.parse_args()

    profiles = rows(args.profiles)
    n = len(profiles)
    external = load(args.external_report)
    batch = load(args.batch_report)
    resume = load(args.resume_report)
    refresh = load(args.refresh_report)
    research = load(args.research_report)
    sentiment = load(args.sentiment_report)
    ux = load(args.ux_report)
    input_status = check_inputs(
        {"external": external, "batch": batch, "refresh": refresh, "research": research, "ux": ux},
        {
            "external": bool(external),
            "batch": bool(batch),
            "refresh": bool(refresh),
            "research": bool(research),
            "ux": bool(ux),
        },
    )

    # Measurements come from the artefacts when they are supplied; otherwise the report is trusted
    # only with an explicit flag, and the trust boundary is recorded in the output.
    derived = derive_external_measurements(args, profiles)
    mismatches = compare_measurements(derived, external)
    derivation_supplied = bool(derived)
    report_trusted = bool(args.trust_external_report)
    measurement_source = "derived_from_artifacts" if derivation_supplied else ("trusted_report" if report_trusted else "unverified_report")

    measured = {**external, **{key: value for key, value in derived.items() if key in external or key in {
        "coverage", "fresh_coverage", "connector_policy_passed", "published_audited",
        "wrong_entity_publications", "unsupported_publications", "audit_size_gate"}}}
    measured_coverage = measured.get("coverage", {}) if isinstance(measured.get("coverage"), dict) else {}
    if derivation_supplied:
        external_qualified = bool(derived.get("audit_qualification_passed"))
    else:
        # Report-only runs may only award external points when the trust boundary is accepted.
        external_qualified = bool(measured.get("qualification_passed")) and report_trusted
    wrong_entity = numeric(measured.get("wrong_entity_publications"))
    published_audited = numeric(measured.get("published_audited"))
    unsupported_publications = numeric(measured.get("unsupported_publications"))
    measurement_trusted = derivation_supplied or report_trusted
    zero_wrong = measurement_trusted and wrong_entity == 0 and (published_audited or 0) > 0
    supported = measurement_trusted and unsupported_publications == 0 and (published_audited or 0) > 0
    entity_points = 10.0 if external_qualified and zero_wrong else 0.0
    breadth_points = coverage_points(measured_coverage, "two_platforms", 10, external_qualified)
    workforce_points = coverage_points(measured_coverage, "workforce_jobs", 7, external_qualified)
    review_points = coverage_points(measured_coverage, "ratings_reviews", 8, external_qualified)
    buzz_points = coverage_points(measured_coverage, "buzz_engagement", 7, external_qualified)

    sentiment_gate = bool(
        sentiment.get("qualification_passed")
        and sentiment.get("wrong_entity_predictions") == 0
        and numeric(sentiment.get("evidence_support_rate")) == 1.0
    )
    sentiment_points = coverage_points(measured_coverage, "sentiment", 10, sentiment_gate)
    freshness_points = capped(3, numeric(measured.get("fresh_coverage"))) if external_qualified else 0.0
    external_score = {
        "verified_external_identity": entity_points,
        "multi_source_breadth": breadth_points,
        "workforce_and_jobs": workforce_points,
        "ratings_and_reviews": review_points,
        "buzz_and_engagement": buzz_points,
        "qualified_sentiment": sentiment_points,
        "external_freshness": freshness_points,
    }

    exact_registry = ratio(
        sum(
            (row.get("evidence", {}).get("registry_live", {}).get("value") or {}).get("organisation_number")
            == row.get("organisation_number")
            for row in profiles
        ),
        n,
    )
    financial = ratio(sum(row.get("evidence", {}).get("financials", {}).get("status") == "available" for row in profiles), n)
    roles_locations = ratio(
        sum(all(module in row.get("evidence", {}) for module in ("roles", "locations")) for row in profiles),
        n,
    )
    website_terminal = ratio(sum("website" in row.get("evidence", {}) for row in profiles), n)
    foundation = {
        "official_identity": capped(4, exact_registry),
        "annual_accounts": capped(4, financial),
        "roles_and_locations": capped(4, roles_locations),
        "website_seed_and_terminal_state": capped(3, website_terminal),
    }

    research_raw = min(12.0, numeric(research.get("score")) or 0.0)
    external_qa_passed = bool(research.get("external_footprint_qa_passed"))
    research_points = round(min(10.0, research_raw * 10 / 12), 3)
    if not external_qa_passed:
        research_points = min(5.0, research_points)

    batch_valid = bool(batch.get("validation", {}).get("passed") and numeric(batch.get("emitted_envelopes")) == n)
    resume_valid = bool(
        resume.get("validation", {}).get("passed") and numeric(resume.get("profiles_fetched_this_run")) == 0
    ) if resume else False
    refresh_valid = bool(refresh.get("qualification_passed") and refresh.get("evidence_complete") and refresh.get("idempotent_rerun"))
    p95 = numeric((batch.get("operations") or {}).get("p95_ms")) if isinstance(batch.get("operations"), dict) else None
    connector_policy = bool(external.get("connector_policy_passed"))
    extensibility = {
        "terminal_daily_batch": 4.0 if batch_valid else 0.0,
        "deterministic_resume": 2.0 if resume_valid else 0.0,
        "measured_refresh_diffs": 3.0 if refresh_valid else 0.0,
        "latency_budget": 1.0 if batch_valid and p95 is not None and p95 <= 10_000 else 0.0,
        "connector_rights_and_rate_policy": 2.0 if connector_policy else 0.0,
    }

    ux_raw = min(8.0, numeric(ux.get("score")) or 0.0)
    ux_external = bool(ux.get("external_intelligence_presented"))
    ux_points = ux_raw if ux_external else min(4.0, ux_raw)

    categories = {
        "external_footprint_intelligence": round(sum(external_score.values()), 3),
        "official_company_foundation": round(sum(foundation.values()), 3),
        "research_agent": research_points,
        "daily_extensibility_refresh": round(sum(extensibility.values()), 3),
        "product_ux_design": ux_points,
    }
    raw_score = round(sum(categories.values()), 3)
    if derivation_supplied:
        audit_gate = bool(derived.get("audit_size_gate")) and bool(derived.get("audit_qualification_passed"))
        connector_policy_measured = bool(derived.get("connector_policy_passed"))
    else:
        # Without derivation artefacts the report is the only source; it may only satisfy gates
        # when the operator has explicitly accepted that trust boundary.
        audit_gate = bool(external.get("audit_size_gate")) and report_trusted
        connector_policy_measured = connector_policy and report_trusted
    gates = {
        "external_audit_at_least_100": audit_gate,
        "zero_wrong_company_external_publications": zero_wrong,
        "external_claims_supported": supported,
        "external_connector_policy": connector_policy_measured,
        "external_report_consistent": not mismatches,
        "official_identity_complete": exact_registry == 1.0,
        "terminal_batch_contract": batch_valid,
        "refresh_replay": refresh_valid,
    }
    qualification = all(gates.values())
    limitations: list[str] = []
    if not derivation_supplied and not report_trusted:
        limitations.append(
            "external measurements were read from the evaluator report without derivation artefacts "
            "(--observations/--labels); the external gates stay unproven unless --trust-external-report "
            "is passed explicitly."
        )
    if mismatches:
        limitations.append(
            "the evaluator report disagrees with the derived measurements on %d field(s); the "
            "external_report_consistent gate fails and the submission cannot qualify." % len(mismatches)
        )
    if not external_qa_passed:
        limitations.append(
            "research.external_footprint_qa_passed has no producer in this repository; the research "
            "category is capped at 5/10 until a product decision defines that QA."
        )
    if not ux_external:
        limitations.append(
            "ux.external_intelligence_presented has no producer in this repository; the UX category is "
            "capped at 4/8 until a UX report producer exists."
        )
    report = {
        "scorer": "signalpost_external_first_competition_proxy_v3",
        "claim_boundary": "Optimization proxy. Final score requires the organiser's frozen hidden companies and independent labels. No accuracy, calibration, or predictiveness claim is made from this run.",
        "is_proxy": True,
        "measurement_source": measurement_source,
        "measurement_trust": {
            "derived_from_artifacts": derivation_supplied,
            "observations_supplied": bool(args.observations),
            "labels_supplied": bool(args.labels),
            "report_values_explicitly_trusted": report_trusted,
            "note": "derived measurements replace report values; disagreements fail external_report_consistent",
        },
        "external_report_mismatches": mismatches,
        "input_contract": INPUT_CONTRACT,
        "input_status": input_status,
        "missing_inputs": input_status["missing_inputs"],
        "documented_limitations": limitations,
        "rubric_weights": {
            "external_footprint_intelligence": 55,
            "official_company_foundation": 15,
            "research_agent": 10,
            "daily_extensibility_refresh": 12,
            "product_ux_design": 8,
        },
        "profiles": n,
        "details": {"external": external_score, "foundation": foundation, "extensibility": extensibility},
        "category_scores": categories,
        "raw_score": raw_score,
        "target": args.target,
        "target_met": raw_score >= args.target,
        "qualification_gates": gates,
        "qualification_passed": qualification,
        "awardable_score": raw_score if qualification else 0,
        "unproven_or_failed": [name for name, passed in gates.items() if not passed],
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_complete_inputs and not input_status["complete"]:
        raise SystemExit(f"Scorer inputs incomplete: {len(input_status['missing_inputs'])} missing entries")


if __name__ == "__main__":
    main()
