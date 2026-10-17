#!/usr/bin/env python3
"""Verify a finished batch run (report + envelopes) before it is published as a smoke-test result.

This is the check to run on a machine that executed ``scripts/run_competition_batch.py`` against
the committed input and the real registry snapshot. It reads only local artefacts, makes no network
request and never alters them. It proves the artefacts are internally consistent and conform to the
documented contract; it does not prove that any claim is true or that the run was executed against
live sources — that provenance is recorded by the operator alongside the tracked report
(``artifacts/README.md``).

Checks (each printed as PASS/FAIL; the exit code is 1 if any check fails):

* the report's ``expected_count``/``emitted_envelopes`` and the envelope file agree with the
  committed organisation list — same count, same organisation numbers, same order, no duplicates;
* ``validation.passed`` and ``contract_status`` say every envelope passed, and every envelope
  re-validates here with ``validate_contract_envelope`` and the public citation checks;
* ``run_id`` and ``contract_version`` are consistent across the report and the envelopes;
* the report's request total equals the sum of the per-envelope request counts (fresh runs only);
* ``operations.third_party_cost_usd`` equals the sum of the per-envelope declarations and the
  accounting is complete;
* the report carries a registry snapshot digest and the requested/selected counts match;
* no credential-like strings or embedded URL credentials appear in the report or the envelopes.

It also prints the information an operator records with the artefact: SHA-256 and size of each
file, the terminal-status and per-module availability distribution (actual module failures are
part of the result and must be preserved, not edited away) and the wall-clock runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import (  # noqa: E402
    CONTRACT_VERSION,
    public_citation_findings,
    read_organisation_numbers,
    validate_contract_envelope,
)

SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|secret|bearer |authorization|password|-----BEGIN )")
URL_CREDENTIAL_PATTERN = re.compile(r"https?://[^/\s\"']*:[^/\s\"']*@")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Checks:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append({"check": name, "passed": bool(passed), "detail": detail})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail and not passed else ""))
        return bool(passed)

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [item for item in self.results if not item["passed"]]


def file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def declared_cost(envelope: dict[str, Any]) -> float | None:
    operations = envelope.get("operations")
    value = operations.get("third_party_cost_usd") if isinstance(operations, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a finished competition batch run before publishing it")
    parser.add_argument("--report", required=True, help="Machine-readable run report written by run_competition_batch.py --report")
    parser.add_argument("--envelopes", required=True, help="Terminal envelope JSONL written by --output")
    parser.add_argument("--organisations", required=True, help="The committed organisation list the run was given")
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--run-id", default=None, help="Require this run id (default: only require consistency)")
    parser.add_argument("--json-output", default=None, help="Optional path for a machine-readable summary of these checks")
    args = parser.parse_args()

    checks = Checks()
    report_path, envelopes_path = Path(args.report), Path(args.envelopes)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        checks.check("report.readable", False, f"{report_path}: {exc}")
        raise SystemExit(1)
    try:
        envelope_lines = [line for line in envelopes_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        envelopes = [json.loads(line) for line in envelope_lines]
    except (OSError, ValueError) as exc:
        checks.check("envelopes.readable", False, f"{envelopes_path}: {exc}")
        raise SystemExit(1)
    expected_orgs = read_organisation_numbers(args.organisations)
    expected = args.expected_count

    # --- counts and membership -----------------------------------------------------------------
    checks.check("input.expected_count", len(expected_orgs) == expected, f"organisation file has {len(expected_orgs)} rows, expected {expected}")
    checks.check("report.expected_count", report.get("expected_count") == expected, f"report.expected_count={report.get('expected_count')}")
    checks.check("report.emitted_envelopes", report.get("emitted_envelopes") == expected, f"report.emitted_envelopes={report.get('emitted_envelopes')}")
    checks.check("envelopes.count", len(envelopes) == expected, f"{len(envelopes)} envelope lines, expected {expected}")
    orgs = [str(item.get("organisation_number")) for item in envelopes]
    checks.check("envelopes.unique_organisations", len(set(orgs)) == len(orgs) == expected, f"{len(set(orgs))} unique of {len(orgs)}")
    checks.check("envelopes.match_input_set", set(orgs) == set(expected_orgs),
                 f"missing={sorted(set(expected_orgs) - set(orgs))[:5]} extra={sorted(set(orgs) - set(expected_orgs))[:5]}")
    checks.check("envelopes.match_input_order", orgs == expected_orgs, "envelopes are not in the input order")

    # --- validation and contract -----------------------------------------------------------------
    validation = report.get("validation") or {}
    checks.check("report.validation_passed", validation.get("passed") is True, json.dumps(validation)[:300])
    status = report.get("contract_status") or {}
    checks.check(
        "report.contract_status",
        status.get("envelopes_validated") == expected and status.get("envelopes_passed") == expected and not status.get("failures"),
        json.dumps({k: status.get(k) for k in ("contract_version", "envelopes_validated", "envelopes_passed", "failures")})[:300],
    )
    checks.check("report.contract_version", status.get("contract_version") == CONTRACT_VERSION, f"{status.get('contract_version')!r} != {CONTRACT_VERSION!r}")
    invalid = [(org, validate_contract_envelope(env)["errors"][:2]) for org, env in zip(orgs, envelopes) if not validate_contract_envelope(env)["passed"]]
    checks.check("envelopes.revalidate", not invalid, f"{len(invalid)} invalid, first: {invalid[:2]}")
    citation = [(org, public_citation_findings(env)[:2]) for org, env in zip(orgs, envelopes) if public_citation_findings(env)]
    checks.check("envelopes.public_citation_checks", not citation, f"{len(citation)} envelope(s) with findings, first: {citation[:2]}")
    versions = {env.get("contract_version") for env in envelopes}
    checks.check("envelopes.contract_version", versions == {CONTRACT_VERSION}, f"{sorted(map(str, versions))}")

    # --- run identity ---------------------------------------------------------------------------
    run_ids = {(env.get("run") or {}).get("run_id") for env in envelopes}
    checks.check("run_id.consistent", run_ids == {report.get("run_id")}, f"report={report.get('run_id')!r} envelopes={sorted(map(str, run_ids))}")
    if args.run_id is not None:
        checks.check("run_id.expected", report.get("run_id") == args.run_id, f"{report.get('run_id')!r} != {args.run_id!r}")
    started, completed = parse_time(report.get("started_at")), parse_time(report.get("completed_at"))
    checks.check("report.timestamps", started is not None and completed is not None and completed >= started,
                 f"started_at={report.get('started_at')!r} completed_at={report.get('completed_at')!r}")

    # --- operations: requests and cost -----------------------------------------------------------
    operations = report.get("operations") or {}
    envelope_requests = sum(int((env.get("operations") or {}).get("requests") or 0) for env in envelopes)
    if report.get("resumed_profiles", 0) == 0:
        checks.check("report.requests_match_envelopes", operations.get("requests") == envelope_requests,
                     f"report.operations.requests={operations.get('requests')} sum(envelope.operations.requests)={envelope_requests}")
    else:
        print(f"[INFO] report.requests_match_envelopes skipped: resumed run ({report.get('resumed_profiles')} profiles reused)")
    costs = [declared_cost(env) for env in envelopes]
    usable = [value for value in costs if value is not None and math.isfinite(value) and value >= 0]
    expected_cost = round(sum(usable), 6) if len(usable) == len(costs) else None
    checks.check(
        "report.cost_aggregate",
        operations.get("third_party_cost_usd_complete") is True
        and expected_cost is not None
        and operations.get("third_party_cost_usd") == expected_cost
        and bool(operations.get("third_party_cost_usd_basis")),
        json.dumps({k: operations.get(k) for k in ("third_party_cost_usd", "third_party_cost_usd_complete", "third_party_cost_usd_basis", "third_party_cost_usd_unavailable_envelopes")}) + f" envelope_sum={expected_cost}",
    )

    # --- registry provenance ---------------------------------------------------------------------
    registry = report.get("registry") or {}
    checks.check(
        "report.registry_metadata",
        bool(HEX64.match(str(registry.get("registry_snapshot_sha256") or ""))) and registry.get("requested") == expected and registry.get("selected") == expected,
        json.dumps(registry)[:300],
    )

    # --- secrets ---------------------------------------------------------------------------------
    report_text = report_path.read_text(encoding="utf-8")
    envelopes_text = envelopes_path.read_text(encoding="utf-8")
    hits = sorted({match.group(0).strip().lower() for text in (report_text, envelopes_text) for match in SECRET_PATTERN.finditer(text)})
    checks.check("artifacts.no_credential_strings", not hits, f"matches: {hits}")
    url_hits = sorted({match.group(0) for text in (report_text, envelopes_text) for match in URL_CREDENTIAL_PATTERN.finditer(text)})
    checks.check("artifacts.no_url_credentials", not url_hits, f"matches: {url_hits[:3]}")

    # --- information to record with the artefact --------------------------------------------------
    print("\nRecord with the artefact:")
    for label, path in (("report", report_path), ("envelopes", envelopes_path)):
        digest, size = file_digest(path)
        print(f"  {label}: sha256={digest} bytes={size} path={path}")
    print(f"  run_id={report.get('run_id')!r} started_at={report.get('started_at')} completed_at={report.get('completed_at')}"
          + (f" runtime_s={(completed - started).total_seconds():.1f}" if started and completed else ""))
    print(f"  registry_snapshot_sha256={registry.get('registry_snapshot_sha256')} rows_scanned={registry.get('registry_rows_scanned')}")
    print(f"  operations={json.dumps(operations)}")
    terminal = Counter((env.get("run") or {}).get("terminal_status") for env in envelopes)
    print(f"  terminal_status={dict(terminal)}")
    availability: dict[str, Counter[str]] = {}
    for env in envelopes:
        for claim in env.get("claims") or []:
            availability.setdefault(str(claim.get("field")), Counter())[str(claim.get("availability"))] += 1
    for module, counter in availability.items():
        print(f"  module {module}: {dict(counter)}")
    external = report.get("external")
    if external is not None:
        print(f"  external observations: accepted={external.get('accepted')} organisations_with_publishable_observation={external.get('organisations_with_publishable_observation')}")
    else:
        print("  external observations: none supplied (official-only run)")

    summary = {
        "verifier": "signalpost_smoke_artifact_check_v1",
        "passed": not checks.failed,
        "checks": checks.results,
        "report_sha256": file_digest(report_path)[0],
        "envelopes_sha256": file_digest(envelopes_path)[0],
    }
    if args.json_output:
        Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_output).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\n{'PASSED' if summary['passed'] else 'FAILED'}: {len(checks.results) - len(checks.failed)}/{len(checks.results)} checks")
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
