#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import (  # noqa: E402
    CONTRACT_VERSION,
    EVIDENCE_STATUS_TO_AVAILABILITY,
    profile_complete_for_modules,
    profiles_from_bulk,
    read_organisation_inputs,
    terminal_envelope,
    validate_contract_envelope,
    validate_envelopes,
)
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.external_pipeline import (  # noqa: E402
    ObservationInputError,
    company_external_block,
    gate_observations,
    read_observation_file,
    run_external_summary,
)
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def load_external_gate(args, orgs: list[str]) -> tuple[dict | None, dict | None]:
    """Read the external observation files and apply the identity/rights/freshness gate.

    Returns ``(gate, summary)`` or ``(None, None)`` when no file was supplied, which keeps the
    historical official-only behaviour byte-for-byte intact.
    """
    if not args.external_observations:
        return None, None
    records: list[dict] = []
    malformed: list[dict] = []
    for source in args.external_observations:
        file_records, file_malformed = read_observation_file(source)
        records.extend(file_records)
        malformed.extend(file_malformed)
    gate = gate_observations(
        records,
        organisation_numbers=orgs,
        malformed=malformed,
        as_of=args.external_as_of,
        freshness_days=args.external_freshness_days,
        enforce_freshness=args.enforce_freshness,
    )
    summary = run_external_summary(gate, organisation_numbers=orgs, enforce_freshness=args.enforce_freshness)
    summary["per_organisation"] = {
        org: {
            "accepted": gate["by_organisation"][org]["accepted_observations"],
            "fresh": gate["by_organisation"][org]["fresh_observations"],
            "stale": gate["by_organisation"][org]["stale_observations"],
            "rejected": len(gate["by_organisation"][org]["rejected"]),
        }
        for org in sorted(gate["by_organisation"])
    }
    return gate, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website")
    parser.add_argument(
        "--external-observations",
        action="append",
        metavar="PATH",
        help="External observation JSONL (repeatable). Records are gated per organisation; omitted files keep the official-only behaviour.",
    )
    parser.add_argument(
        "--external-as-of",
        default=None,
        help="ISO-8601 timestamp used as 'now' for the freshness policy (default: wall clock)",
    )
    parser.add_argument("--external-freshness-days", type=int, default=45, help="Freshness window in days")
    parser.add_argument(
        "--enforce-freshness",
        action="store_true",
        help="Count only fresh accepted observations in the external footprint; stale records stay visible but are not counted",
    )
    args = parser.parse_args()

    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}

    def enrich(profile: dict) -> tuple[dict, dict]:
        profile_started = time.monotonic()
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
        profile["evidence"].update(records)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules:
            website_record, website_metrics = fetch_website(profile.get("website"))
            profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
        metric = {
            "requests": len(metrics) + website_metrics["requests"],
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"],
            "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"],
            "runtime_ms": int((time.monotonic() - profile_started) * 1000),
        }
        profile["run_metrics"] = metric
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    discard_stale_external = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        # External evidence is never reused across runs. A resumed profile may carry an
        # ``external_footprint`` block written by a previous invocation; that block was validated
        # under different inputs, at a different time, and possibly for a different rights/
        # freshness policy. Publishing it again would present evidence this run did not validate,
        # so it is dropped here and rebuilt only from this invocation's gated observations.
        for profile in state.values():
            if profile.pop("external_footprint", None) is not None:
                discard_stale_external += 1
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()

    # External observations are gated once, per organisation, before any profile is published.
    try:
        gate, external_summary = load_external_gate(args, orgs)
    except ObservationInputError as exc:
        raise SystemExit(str(exc)) from exc
    external_by_org: dict[str, dict] = {}
    if gate is not None:
        for org in orgs:
            block = company_external_block(gate, org)
            if block is not None:
                external_by_org[org] = block
                state[org]["external_footprint"] = block

    ordered_profiles = [state[org] for org in orgs]
    envelopes = [
        terminal_envelope(
            profile,
            run_id=args.run_id,
            modules=requested_modules,
            started_at=started_at,
            completed_at=completed_at,
            external_block=external_by_org.get(profile["organisation_number"]),
        )
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, args.expected_count)
    contract_results = [
        {"organisation_number": envelope["organisation_number"], **validate_contract_envelope(envelope)}
        for envelope in envelopes
    ]
    contract_failures = [item for item in contract_results if not item["passed"]]
    contract_status = {
        "contract_version": CONTRACT_VERSION,
        "envelopes_validated": len(contract_results),
        "envelopes_passed": len(contract_results) - len(contract_failures),
        "failures": contract_failures[:20],
        "availability_mapping": dict(EVIDENCE_STATUS_TO_AVAILABILITY),
        "terminal_status_values": ["completed", "failed"],
        "external_block_present": len(external_by_org),
    }
    resume_status = {
        "profiles_reused": resumed_profiles,
        "stale_external_blocks_discarded": discard_stale_external,
        "policy": "external evidence is rebuilt only from this invocation's gated observations; previously serialized external blocks are never reused",
    }
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "validation": validation,
        "contract_status": contract_status,
    }
    if external_summary is not None:
        report["external"] = external_summary
    if args.resume:
        report["resume"] = resume_status
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] and not contract_failures else 1)


if __name__ == "__main__":
    main()
