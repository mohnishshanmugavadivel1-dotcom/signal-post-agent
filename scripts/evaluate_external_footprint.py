#!/usr/bin/env python3
"""Audit frozen external observations and report the competition proxy's external inputs.

This script is the producer for the external report consumed by
``scripts/score_competition_v3.py``. Two of the keys that scorer reads —
``fresh_coverage`` and ``connector_policy_passed`` — had no producer before this change, so the
proxy's connector-policy gate could never pass. Definitions used here:

* ``fresh_coverage`` — share of profiles with at least one accepted observation inside the
  freshness window (``--as-of`` minus ``--freshness-days``). The scorer multiplies it by the
  rubric's 3 freshness points.
* ``connector_policy_passed`` — at least one observation is publishable, every published record
  declares an approved acquisition mode and approved rights, and no published record used an
  experimental mode. This is a record-level policy/consistency gate; it does **not** verify
  platform terms, which remain a human/legal declaration.

The audit gate (``qualification_passed``) is unchanged: it still requires an independent labelled
audit of at least ``--minimum-audit`` records with zero wrong-entity publications.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.external_footprint import (  # noqa: E402
    EXPERIMENTAL_ACQUISITION_MODES,
    PUBLISHABLE_ACQUISITION_MODES,
    parse_timestamp,
    publishable_observation,
)
from norway_company_agent.external_pipeline import audit_records, coverage_from_observations  # noqa: E402


def read_jsonl(path: Path, *, strict: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read JSONL, reporting malformed lines instead of aborting (unless ``strict``).

    Returns ``(rows, malformed)`` so a partially corrupted observation file still produces a report
    and the corruption stays visible in it. Label files are read strictly: audit labels are the
    measurement, so a broken label file must fail loudly.
    """
    rows: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if strict:
                raise ValueError(f"{path}: line {number} is not valid JSON: {exc.msg}") from exc
            malformed.append({"file": str(path), "line": number, "error": f"invalid JSON: {exc.msg}"})
            continue
        if not isinstance(value, dict):
            malformed.append({"file": str(path), "line": number, "error": f"record is {type(value).__name__}, expected object"})
            continue
        rows.append(value)
    return rows, malformed


def is_fresh(item: dict[str, Any], *, as_of: str | None, freshness_days: int) -> bool:
    now = parse_timestamp(as_of)
    retrieved = parse_timestamp(item.get("retrieved_at"))
    if now is None or retrieved is None:
        return False
    delta = (now - retrieved).total_seconds()
    return 0 <= delta <= freshness_days * 86_400


def connector_policy(accepted: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    modes = sorted({str(item.get("acquisition_mode")) for item in accepted})
    rights = sorted({str(item.get("rights_status")) for item in accepted})
    experimental = sorted(set(modes) & EXPERIMENTAL_ACQUISITION_MODES)
    unapproved = sorted(set(modes) - PUBLISHABLE_ACQUISITION_MODES)
    passed = bool(accepted and rights == ["approved"] and not experimental and not unapproved)
    return passed, {
        "definition": "published observations must use approved acquisition modes and approved rights; experimental output never publishes",
        "acquisition_modes_published": modes,
        "rights_statuses_published": rights,
        "experimental_modes_published": experimental,
        "unapproved_modes_published": unapproved,
        "platform_terms_verified_here": False,
        "note": "Platform terms and licence acceptance remain a human declaration; this gate only proves the record-level policy holds.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate frozen external-footprint observations and exact-entity labels.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument(
        "--labels",
        required=True,
        help="JSONL audit labels keyed by composite identity: organisation_number, id, exact_entity, metric_correct, sentiment_correct",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--minimum-audit", type=int, default=100)
    parser.add_argument("--as-of", default=None, help="ISO-8601 'now' for the freshness window (default: each record's own retrieval time cannot be evaluated, so the wall clock is used)")
    parser.add_argument("--freshness-days", type=int, default=45)
    args = parser.parse_args()

    profiles, malformed_profiles = read_jsonl(Path(args.profiles))
    observations, malformed_observations = read_jsonl(Path(args.observations))
    label_rows, _ = read_jsonl(Path(args.labels), strict=True)
    all_orgs = {str(item["organisation_number"]) for item in profiles}

    # Audit measurement is derived from the observation and label artefacts, keyed by
    # (organisation_number, id). A label can therefore never be applied to another
    # organisation's record that happens to reuse the same observation id.
    audit = audit_records(
        observations,
        label_rows,
        minimum_audit=args.minimum_audit,
        organisation_numbers=all_orgs,
    )
    if audit["labels_invalid"] or audit["labels_conflicting"]:
        # Broken or contradictory audit labels must not be silently tolerated: they invalidate the
        # measurement they were meant to support.
        print(
            "Refusing to qualify: %d invalid label(s), %d conflicting label(s)"
            % (len(audit["labels_invalid"]), len(audit["labels_conflicting"])),
            file=sys.stderr,
        )
    # Only observations for organisations in the frozen batch can count toward coverage; anything
    # else is reported separately so an out-of-batch record can never inflate a company's coverage.
    in_batch = [item for item in observations if str(item.get("organisation_number")) in all_orgs]
    out_of_batch = len(observations) - len(in_batch)
    accepted_all = [item for item in in_batch if publishable_observation(item)]
    fresh_all = [item for item in accepted_all if is_fresh(item, as_of=args.as_of, freshness_days=args.freshness_days)]
    fresh_object_ids = {id(item) for item in fresh_all}
    stale_all = [item for item in accepted_all if id(item) not in fresh_object_ids]

    # Coverage shares come from the shared helper so the evaluator and the competition proxy
    # cannot report different coverage for the same artefacts.
    coverage = coverage_from_observations(
        observations,
        organisation_numbers=all_orgs,
        as_of=args.as_of,
        freshness_days=args.freshness_days,
    )
    acquisition_modes = Counter(str(item.get("acquisition_mode")) for item in observations)
    qualification = bool(audit["qualification_passed"] and not audit["labels_invalid"] and not audit["labels_conflicting"])
    fresh_coverage = coverage["fresh"]
    policy_passed, policy_detail = connector_policy(accepted_all)
    report = {
        "scorer": "signalpost_external_footprint_eval_v1",
        "claim_boundary": "Held-out observation audit plus full-corpus coverage; it does not validate an unlabelled connector.",
        "profiles": len(profiles),
        "observations": len(observations),
        "malformed_profiles": malformed_profiles,
        "malformed_observations": malformed_observations,
        "observations_in_batch": len(in_batch),
        "observations_out_of_batch": out_of_batch,
        "audit_identity_policy": audit["identity_policy"],
        "audited_observations": audit["audited_observations"],
        "published_audited": audit["published_audited"],
        "wrong_entity_publications": audit["wrong_entity_publications"],
        "wrong_metric_publications": audit["wrong_metric_publications"],
        "unsupported_publications": audit["unsupported_publications"],
        "entity_precision": audit["entity_precision"],
        "metric_precision": audit["metric_precision"],
        "sentiment_audited": audit["sentiment_audited"],
        "sentiment_accuracy": audit["sentiment_accuracy"],
        "labels_total": audit["labels_total"],
        "labels_usable": audit["labels_usable"],
        "labels_invalid": audit["labels_invalid"],
        "labels_duplicate": audit["labels_duplicate"],
        "labels_conflicting": audit["labels_conflicting"],
        "labels_without_observation": audit["labels_without_observation"],
        "observations_without_labels": audit["observations_without_labels"],
        "audited_unpublished": audit["audited_unpublished"],
        "audit_coverage_by_organisation": audit["coverage_by_organisation"],
        "coverage": coverage,
        "platform_counts": dict(Counter(str(item.get("platform")) for item in accepted_all)),
        "acquisition_modes": dict(acquisition_modes),
        "minimum_audit": args.minimum_audit,
        "audit_size_gate": audit["audit_size_gate"],
        "audit_size": audit["audit_size"],
        "qualification_passed": qualification,
        # --- inputs consumed by scripts/score_competition_v3.py -------------------------------
        "fresh_coverage": fresh_coverage,
        "connector_policy_passed": policy_passed,
        "connector_policy": policy_detail,
        "stale_observations": len(stale_all),
        "fresh_observations": len(fresh_all),
        "freshness": {
            "as_of": args.as_of or "wall clock",
            "freshness_days": args.freshness_days,
            "counts_scope": "accepted for coverage keys; fresh for fresh_coverage",
            "undated_is_fresh": False,
        },
        "hash_verification": {
            "mode": "syntax_and_content_duplicate",
            "recomputed_from_bytes": False,
            "limitation": "Observation records do not carry fetched bytes; the gate cannot recompute the content digest.",
        },
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
