#!/usr/bin/env python3
"""Self-checking end-to-end verification of the competition pipeline.

Runs the real competition CLI (``scripts/run_competition_batch.py``) plus the external-footprint
evaluator and the competition proxy as subprocesses, in a temporary work directory, against the
synthetic fixtures in ``tests/fixtures``. Every check is an explicit assertion with a printed
diagnostic, so a failure explains itself. Exit code is non-zero when any critical check fails.

Nothing here touches the network, the production registry snapshot, or any directory outside the
temporary work directory. No check depends on a previous run: the work directory is created fresh
unless ``--workdir`` is given.

Usage:
    python scripts/verify_competition_e2e.py [--root PATH] [--workdir PATH] [--timeout 120]

Verification steps
------------------
 1  contract: every emitted envelope validates against the documented output contract
 2  acceptance: a valid observation for the right organisation reaches that profile only
 3  rejection: invalid rights are refused and reported with a machine-readable reason
 4  entity: mismatched entities and unlisted organisations never reach publishable output
 5  duplicates: identical duplicates collapse, conflicting duplicates are withheld
 6  freshness: stale records stay visible, are reported separately, and can be excluded
 7  malformed: broken JSONL lines are reported with file and line, never dropped silently
 8  aggregation: counts, platform breakdown and per-organisation totals agree with the records
 9  separation: official evidence and external observations never mix
10  isolate: no organisation's external block contains another organisation's observation
11  contract-api: validate_contract_envelope accepts the output and rejects tampered variants
12  scorer: proxy input contract reports missing inputs and can fail hard on request
13  determinism: two runs produce identical external summaries for the same inputs
14  negative control: deliberately tampered expectations must be detected as failures
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import validate_contract_envelope  # noqa: E402

ORGS_FIXTURE = Path("tests/fixtures/batch-orgs-3.jsonl")
BULK_FIXTURE = Path("tests/fixtures/bulk-registry-sample.csv.gz")
OBSERVATIONS_VALID = Path("tests/fixtures/external-observations-valid.jsonl")
OBSERVATIONS_ADVERSARIAL = Path("tests/fixtures/external-observations-adversarial.jsonl")
AUDIT_LABELS = Path("tests/fixtures/external-audit-labels.synthetic.jsonl")
AS_OF = "2026-08-24T00:00:00Z"
MODULES = "registry,accounting_obligation,website"

CRITICAL = "critical"
WARNING = "warning"


class Findings:
    """Collects check results. A single critical finding makes the runner fail."""

    def __init__(self, *, quiet: bool = False) -> None:
        self.entries: list[dict[str, str]] = []
        self.quiet = quiet

    def check(self, name: str, passed: bool, detail: str = "", severity: str = CRITICAL) -> bool:
        self.entries.append({"check": name, "status": "pass" if passed else "fail", "severity": severity if not passed else "-", "detail": detail})
        if not self.quiet:
            marker = "PASS" if passed else "FAIL"
            print(f"[{marker}] {name}" + (f" — {detail}" if detail and not passed else ""))
        return passed

    @property
    def critical_failures(self) -> list[dict[str, str]]:
        return [entry for entry in self.entries if entry["status"] == "fail" and entry["severity"] == CRITICAL]

    @property
    def failures(self) -> list[dict[str, str]]:
        return [entry for entry in self.entries if entry["status"] == "fail"]


def run(command: list[str], *, timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def run_batch(
    python: str,
    root: Path,
    workdir: Path,
    *,
    label: str,
    observations: list[Path],
    timeout: int,
    enforce_freshness: bool = False,
) -> dict[str, Any]:
    """Run the competition CLI once and return the parsed artifacts."""
    profiles = workdir / f"profiles-{label}.jsonl"
    envelopes = workdir / f"envelopes-{label}.jsonl"
    report = workdir / f"report-{label}.json"
    command = [
        python, str(root / "scripts" / "run_competition_batch.py"),
        "--organisations", str(root / ORGS_FIXTURE),
        "--bulk", str(root / BULK_FIXTURE),
        "--profiles-output", str(profiles),
        "--output", str(envelopes),
        "--report", str(report),
        "--run-id", f"e2e-{label}",
        "--expected-count", "3",
        "--modules", MODULES,
        "--external-as-of", AS_OF,
    ]
    for path in observations:
        command += ["--external-observations", str(path)]
    if enforce_freshness:
        command.append("--enforce-freshness")
    completed = run(command, timeout=timeout, cwd=root)
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "profiles_path": profiles,
        "envelopes_path": envelopes,
        "report_path": report,
        "profiles": read_jsonl(profiles) if profiles.exists() else [],
        "envelopes": read_jsonl(envelopes) if envelopes.exists() else [],
        "report": json.loads(report.read_text(encoding="utf-8")) if report.exists() else {},
    }


def acceptance_expectation(report: dict[str, Any]) -> bool:
    """The positive expectation the runner places on a run (control input must violate it)."""
    return (report.get("external") or {}).get("accepted", 0) > 0


def check_contract(findings: Findings, run_result: dict[str, Any]) -> None:
    if not findings.check("cli.exit_code_zero", run_result["returncode"] == 0, f"exit={run_result['returncode']} stderr={run_result['stderr'][-400:]}"):
        return
    findings.check("artifacts.parsed", bool(run_result["envelopes"]) and bool(run_result["profiles"]) and bool(run_result["report"]),
                   f"envelopes={len(run_result['envelopes'])} profiles={len(run_result['profiles'])} report={bool(run_result['report'])}")
    problems: list[str] = []
    for envelope in run_result["envelopes"]:
        result = validate_contract_envelope(envelope)
        if not result["passed"]:
            problems.append(f"{envelope.get('organisation_number')}: {result['errors'][:3]}")
    findings.check("contract.envelopes_valid", not problems, "; ".join(problems))
    status = run_result["report"].get("contract_status", {})
    findings.check(
        "contract.report_status",
        status.get("envelopes_validated") == 3 and not status.get("failures") and bool(status.get("contract_version")),
        json.dumps(status)[:400],
    )
    required = {"contract_version", "organisation_number", "run", "claims", "evidence", "changes", "errors", "operations"}
    missing = [sorted(required - set(envelope)) for envelope in run_result["envelopes"]]
    findings.check("contract.required_fields", not any(missing), f"missing={missing}")


def check_acceptance_and_rejection(findings: Findings, run_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_org = {envelope["organisation_number"]: envelope for envelope in run_result["envelopes"]}
    summary = run_result["report"].get("external", {})
    third = by_org.get("123456789", {})
    findings.check("acceptance.valid_observation_reaches_right_org",
                   "places-summary-923609016" in (by_org.get("923609016", {}).get("external") or {}).get("accepted_observation_ids", []),
                   "places-summary-923609016 missing from the 923609016 external block")
    findings.check("acceptance.unlisted_org_has_no_block", "external" not in third,
                   f"unexpected external block for 123456789: {third.get('external')}")
    unmatched = {entry["id"]: entry["reasons"] for entry in summary.get("unmatched_records", [])}
    findings.check("rejection.unmatched_org_reported",
                   unmatched == {"unlisted-org": ["organisation number is not part of this batch"]},
                   json.dumps(unmatched))
    malformed_org = [entry for entry in summary.get("unmatched_records", []) if entry["id"] == "short-org"]
    malformed_org_rejections = summary.get("rejection_reasons", {}).get("missing or invalid organisation number", 0)
    findings.check("rejection.malformed_org_rejected_as_data_error",
                   not malformed_org and malformed_org_rejections >= 1,
                   f"unmatched={malformed_org} malformed_org_rejections={malformed_org_rejections}")
    rejected_ids = {
        entry["id"]: entry["reasons"]
        for envelope in run_result["envelopes"]
        for entry in ((envelope.get("external") or {}).get("rejected_observations") or [])
    }
    findings.check("rejection.invalid_rights_refused",
                   "source rights are not approved" in rejected_ids.get("unapproved-rights", []),
                   f"unapproved-rights reasons: {rejected_ids.get('unapproved-rights')}")
    published_ids = {
        observation_id
        for envelope in run_result["envelopes"]
        for observation_id in (envelope.get("external") or {}).get("accepted_observation_ids", [])
    }
    forbidden = {"unapproved-rights", "experimental-mode", "non-hex-hash", "missing-hash", "bad-platform", "missing-span", "not-exact-entity", "missing-proof", "unlisted-org", "dup-conflict"}
    findings.check("publishability.rejected_ids_never_published", not (published_ids & forbidden),
                   f"rejected observations leaked into publishable output: {sorted(published_ids & forbidden)}")
    reasons = run_result["report"].get("external", {}).get("rejection_reasons", {})
    findings.check("rejection.reasons_observable",
                   reasons.get("invalid content hash") == 1 and reasons.get("missing content hash") == 1
                   and reasons.get("exact legal entity is not verified") == 1
                   and reasons.get("missing exact-entity proof") == 1
                   and reasons.get("unsupported platform") == 1
                   and reasons.get("missing evidence span") == 1
                   and reasons.get("acquisition mode is not approved for publication") == 1,
                   json.dumps(reasons))
    return by_org


def check_duplicates_and_freshness(findings: Findings, run_result: dict[str, Any], by_org: dict[str, dict[str, Any]]) -> None:
    summary = run_result["report"]["external"]
    findings.check("duplicates.identical_collapsed", summary.get("duplicates_collapsed") == 1, json.dumps({k: summary.get(k) for k in ("duplicates_collapsed", "records_read", "unique_records")}))
    findings.check("duplicates.conflict_withheld", summary.get("conflicting_duplicate_groups") == 1 and summary.get("conflicting_duplicate_records") == 2, json.dumps(summary.get("duplicate_conflicts")))
    findings.check("duplicates.shared_id_across_orgs_not_lost", summary.get("ids_shared_across_organisations") == ["shared-1"], json.dumps(summary.get("ids_shared_across_organisations")))
    publishable_shared = [
        envelope["organisation_number"]
        for envelope in run_result["envelopes"]
        if "shared-1" in (envelope.get("external") or {}).get("accepted_observation_ids", [])
    ]
    findings.check("duplicates.shared_id_published_for_each_org", publishable_shared == ["923609016", "987654321"], f"published for {publishable_shared}")
    block = by_org["987654321"]["external"]
    findings.check("freshness.stale_visible_but_separate",
                   block["stale_observation_ids"] == ["stale-job"] and "stale-job" in block["accepted_observation_ids"],
                   json.dumps({"stale": block["stale_observation_ids"], "accepted": block["accepted_observation_ids"]}))
    findings.check("freshness.counts_reported",
                   summary.get("stale_observations") == 1 and summary.get("fresh_observations") == 6
                   and block["footprint"]["freshness_policy"]["as_of"].startswith("2026-08-24"),
                   json.dumps({k: summary.get(k) for k in ("stale_observations", "fresh_observations")}))
    malformed = summary.get("malformed_records", [])
    findings.check("malformed.reported_with_line",
                   len(malformed) == 2
                   and {entry["line"] for entry in malformed} == {15, 18}
                   and all(entry.get("file") for entry in malformed)
                   and any("invalid JSON" in entry["error"] for entry in malformed)
                   and any("expected object" in entry["error"] for entry in malformed),
                   json.dumps(malformed))


def check_aggregation(findings: Findings, run_result: dict[str, Any]) -> None:
    summary = run_result["report"]["external"]
    per_org = summary.get("per_organisation", {})
    total = sum(entry["accepted"] for entry in per_org.values())
    findings.check("aggregation.per_org_matches_total", total == summary.get("accepted"), f"per-org={total} total={summary.get('accepted')}")
    envelope_ids = [
        observation_id
        for envelope in run_result["envelopes"]
        for observation_id in (envelope.get("external") or {}).get("accepted_observation_ids", [])
    ]
    # Observation ids are unique per organisation, not globally: the same id may legitimately be
    # published for two organisations, so uniqueness is checked inside each envelope.
    per_envelope_unique = all(
        len(ids) == len(set(ids))
        for envelope in run_result["envelopes"]
        for ids in [[observation_id for observation_id in (envelope.get("external") or {}).get("accepted_observation_ids", [])]]
    )
    findings.check("aggregation.envelopes_match_summary",
                   len(envelope_ids) == summary.get("accepted") and per_envelope_unique,
                   f"envelope ids={len(envelope_ids)} accepted={summary.get('accepted')} per_envelope_unique={per_envelope_unique}")
    counts_ok = True
    detail = ""
    for envelope in run_result["envelopes"]:
        block = envelope.get("external")
        if not block:
            continue
        footprint = block["footprint"]
        if footprint["accepted_observations"] != len(block["accepted_observation_ids"]):
            counts_ok = False
            detail += f"{envelope['organisation_number']}: accepted={footprint['accepted_observations']} ids={len(block['accepted_observation_ids'])}; "
        if sum(footprint["platform_counts"].values()) != footprint["counted_observations"]:
            counts_ok = False
            detail += f"{envelope['organisation_number']}: platform_counts={footprint['platform_counts']} counted={footprint['counted_observations']}; "
    findings.check("aggregation.footprint_matches_records", counts_ok, detail)
    findings.check("aggregation.rejected_entries_are_lean",
                   all(set(entry) == {"id", "reasons"} for envelope in run_result["envelopes"] for entry in ((envelope.get("external") or {}).get("rejected_observations") or [])),
                   "rejected observation stubs must only carry id and reasons")


def check_separation(findings: Findings, run_result: dict[str, Any]) -> None:
    leaked: list[str] = []
    external_hosts = ("maps.example.invalid", "nyheter.example.invalid", "jobs.example.invalid", "linkedin.example.invalid")
    for envelope in run_result["envelopes"]:
        official = envelope["profile"].get("evidence", {})
        overlap = sorted(set(official) & {"external", "external_footprint", "observations"})
        if overlap:
            leaked.append(f"{envelope['organisation_number']} evidence keys {overlap}")
        if any(host in json.dumps(official) for host in external_hosts):
            leaked.append(f"{envelope['organisation_number']} official evidence mentions an external host")
        if "external" in json.dumps(envelope["evidence"]):
            leaked.append(f"{envelope['organisation_number']} contract evidence array mentions the external block")
    findings.check("separation.official_evidence_untouched", not leaked, "; ".join(leaked))
    findings.check("separation.documented_availability_mapping",
                   run_result["report"]["contract_status"]["availability_mapping"]["not_fetched"] == "not_available",
                   json.dumps(run_result["report"]["contract_status"]["availability_mapping"]))


def check_cross_org_isolation(findings: Findings, run_result: dict[str, Any], orgs_by_observation: dict[str, set[str]]) -> None:
    """An observation may only be published for organisations that actually supplied it.

    ``orgs_by_observation`` maps an observation id to every organisation that declared it in the
    fixtures, because the same id is allowed to exist for several organisations.
    """
    violations: list[str] = []
    for envelope in run_result["envelopes"]:
        owner = envelope["organisation_number"]
        for observation_id in (envelope.get("external") or {}).get("accepted_observation_ids", []):
            owners = orgs_by_observation.get(observation_id)
            if owners is not None and owner not in owners:
                violations.append(f"{observation_id} belongs to {sorted(owners)} but appears in {owner}")
    findings.check("isolation.no_cross_org_contamination", not violations, "; ".join(violations))


def check_malformed_record_tolerance(findings: Findings, python: str, root: Path, workdir: Path, timeout: int) -> None:
    """One malformed record must be rejected, and the valid records around it must still process."""
    bad = {
        "id": "bad-metrics", "organisation_number": "923609016", "platform": "company_site",
        "signal_type": "public_post", "source_url": "https://www.example.invalid/nyheter/bad",
        "retrieved_at": "2026-08-22T09:05:00Z", "content_sha256": "a" * 64, "exact_entity": True,
        "identity_proof": [{"type": "gate"}], "acquisition_mode": "permitted_public_page",
        "rights_status": "approved", "source_class": "company_site", "evidence_span": "span",
        "metrics": "not-a-dict",
    }
    good = {
        "id": "good-after-bad", "organisation_number": "923609016", "platform": "company_site",
        "signal_type": "public_post", "source_url": "https://www.example.invalid/nyheter/good",
        "retrieved_at": "2026-08-22T09:06:00Z", "content_sha256": "b" * 64, "exact_entity": True,
        "identity_proof": [{"type": "gate"}], "acquisition_mode": "permitted_public_page",
        "rights_status": "approved", "source_class": "company_site", "evidence_span": "span",
        "metrics": {"likes": 3},
    }
    path = workdir / "mixed-quality.jsonl"
    path.write_text(json.dumps(bad) + "\n" + json.dumps(good) + "\n", encoding="utf-8")
    result = run_batch(python, root, workdir, label="mixed-quality", observations=[path], timeout=timeout)
    findings.check("typing.malformed_record_does_not_abort_run", result["returncode"] == 0,
                   f"exit={result['returncode']} stderr={result['stderr'][-300:]}")
    block = (result["envelopes"][0].get("external") or {}) if result["envelopes"] else {}
    rejected = {entry["id"]: entry["reasons"] for entry in block.get("rejected_observations", [])}
    findings.check("typing.malformed_record_rejected_with_reason",
                   "metrics must be an object" in rejected.get("bad-metrics", []),
                   json.dumps(rejected))
    findings.check("typing.valid_record_after_malformed_is_accepted",
                   "good-after-bad" in block.get("accepted_observation_ids", []),
                   f"accepted={block.get('accepted_observation_ids')}")


def check_resume_external_policy(findings: Findings, python: str, root: Path, workdir: Path, timeout: int) -> None:
    """A resumed run must never republish external evidence from an earlier invocation."""
    resume_work = workdir / "resume"
    resume_work.mkdir(exist_ok=True)
    profiles = resume_work / "profiles.jsonl"

    def invoke(tag, extra):
        command = [
            python, str(root / "scripts" / "run_competition_batch.py"),
            "--organisations", str(root / ORGS_FIXTURE), "--bulk", str(root / BULK_FIXTURE),
            "--profiles-output", str(profiles), "--output", str(resume_work / f"envelopes-{tag}.jsonl"),
            "--report", str(resume_work / f"report-{tag}.json"), "--run-id", f"resume-{tag}",
            "--expected-count", "3", "--modules", MODULES, "--external-as-of", AS_OF,
        ] + extra
        completed = run(command, timeout=timeout, cwd=root)
        envelopes = read_jsonl(resume_work / f"envelopes-{tag}.jsonl") if (resume_work / f"envelopes-{tag}.jsonl").exists() else []
        report = json.loads((resume_work / f"report-{tag}.json").read_text()) if (resume_work / f"report-{tag}.json").exists() else {}
        return completed, envelopes, report

    first, first_envelopes, first_report = invoke("first", ["--external-observations", str(root / OBSERVATIONS_VALID)])
    findings.check("resume.run_one_publishes_external_blocks",
                   first.returncode == 0 and first_report.get("contract_status", {}).get("external_block_present") == 2,
                   f"exit={first.returncode} present={first_report.get('contract_status', {}).get('external_block_present')}")

    second, second_envelopes, second_report = invoke("second", ["--resume"])
    stale = [envelope["organisation_number"] for envelope in second_envelopes if "external_footprint" in envelope.get("profile", {})]
    top_level = [envelope["organisation_number"] for envelope in second_envelopes if "external" in envelope]
    findings.check("resume.no_stale_external_evidence_after_resume",
                   second.returncode == 0 and not stale and not top_level and "external" not in second_report,
                   f"exit={second.returncode} profile_blocks={stale} envelope_blocks={top_level}")
    findings.check("resume.stale_blocks_reported_as_discarded",
                   second_report.get("resume", {}).get("stale_external_blocks_discarded") == 2
                   and second_report.get("contract_status", {}).get("external_block_present") == 0,
                   json.dumps(second_report.get("resume")))

    third, third_envelopes, third_report = invoke("third", ["--resume", "--external-observations", str(root / OBSERVATIONS_VALID)])
    rebuilt = sorted(
        observation_id for envelope in third_envelopes
        for observation_id in ((envelope.get("external") or {}).get("accepted_observation_ids") or [])
    )
    findings.check("resume.blocks_rebuilt_from_current_input",
                   third.returncode == 0 and "places-summary-923609016" in rebuilt
                   and third_report.get("contract_status", {}).get("external_block_present") == 2,
                   f"exit={third.returncode} rebuilt={rebuilt}")


def check_contract_api_negative_cases(findings: Findings, run_result: dict[str, Any]) -> None:
    """Tamper with a valid envelope in memory and require the validator to reject each variant."""
    valid = copy.deepcopy(run_result["envelopes"][0])
    tampered: list[tuple[str, dict[str, Any]]] = []
    fake_hash = copy.deepcopy(valid)
    fake_hash["evidence"][0]["content_sha256"] = "z" * 64
    tampered.append(("evidence hash no longer hex", fake_hash))
    dangling = copy.deepcopy(valid)
    dangling["claims"][0]["evidence_ids"] = ["ev-does-not-exist"]
    tampered.append(("claim points at unknown evidence", dangling))
    count_mismatch = copy.deepcopy(valid)
    count_mismatch["claims"] = count_mismatch["claims"][:-1]
    tampered.append(("claims/evidence count mismatch", count_mismatch))
    ops_mismatch = copy.deepcopy(valid)
    ops_mismatch["operations"]["requests"] = 12345
    tampered.append(("operations disagree with profile metrics", ops_mismatch))
    if valid.get("external"):
        wrong_org = copy.deepcopy(valid)
        wrong_org["external"]["organisation_number"] = "000000000"
        tampered.append(("external block from another organisation", wrong_org))
        not_publishable = copy.deepcopy(valid)
        not_publishable["external"]["publishable_only"] = False
        tampered.append(("external block marked as not publishable-only", not_publishable))
    undetected = [name for name, envelope in tampered if validate_contract_envelope(envelope)["passed"]]
    findings.check("contract.api_rejects_tampered_envelopes", not undetected, f"validator accepted tampered variants: {undetected}")


def check_determinism(findings: Findings, first: dict[str, Any], second: dict[str, Any]) -> None:
    def stable(result: dict[str, Any]) -> str:
        summary = result["report"].get("external", {})
        subset = {
            key: summary.get(key)
            for key in ("records_read", "unique_records", "accepted", "rejected", "unmatched", "malformed",
                        "duplicates_collapsed", "conflicting_duplicate_groups", "fresh_observations",
                        "stale_observations", "fresh_coverage", "rejection_reasons", "per_organisation")
        }
        blocks = [
            (envelope["organisation_number"], (envelope.get("external") or {}).get("accepted_observation_ids"))
            for envelope in result["envelopes"]
        ]
        return json.dumps({"summary": subset, "blocks": blocks}, sort_keys=True)

    findings.check("determinism.two_runs_identical", stable(first) == stable(second),
                   "external summaries differ between two runs with identical inputs")


def check_scorer(findings: Findings, python: str, root: Path, workdir: Path, run_result: dict[str, Any], timeout: int) -> None:
    profiles_path = run_result["profiles_path"]
    eval_output = workdir / "ext-eval.json"
    observations_all = workdir / "observations-all.jsonl"
    observations_all.write_bytes((root / OBSERVATIONS_VALID).read_bytes() + (root / OBSERVATIONS_ADVERSARIAL).read_bytes())
    evaluator = run([
        python, str(root / "scripts" / "evaluate_external_footprint.py"),
        "--profiles", str(profiles_path),
        "--observations", str(observations_all),
        "--labels", str(root / AUDIT_LABELS),
        "--as-of", AS_OF,
        "--output", str(eval_output),
    ], timeout=timeout, cwd=root)
    if not findings.check("scorer.evaluator_runs", evaluator.returncode == 0 and eval_output.exists(), evaluator.stderr[-400:]):
        return
    evaluation = json.loads(eval_output.read_text(encoding="utf-8"))
    findings.check("scorer.evaluator_reports_policy_inputs",
                   isinstance(evaluation.get("fresh_coverage"), (int, float))
                   and evaluation.get("connector_policy_passed") is True
                   and len(evaluation.get("malformed_observations", [])) == 2,
                   json.dumps({k: evaluation.get(k) for k in ("fresh_coverage", "connector_policy_passed", "observations_in_batch", "observations_out_of_batch")}))
    findings.check("scorer.audit_gate_requires_100_records",
                   evaluation.get("audit_size_gate") is False and evaluation.get("qualification_passed") is False,
                   "a 3-record synthetic label file must not satisfy the 100-record audit gate")

    refresh_report = workdir / "refresh-report.json"
    refresh = run([
        python, str(root / "scripts" / "run_refresh_replay.py"),
        "--manifest", str(root / "tests" / "fixtures" / "refresh-snapshots.json"),
        "--output", str(refresh_report),
    ], timeout=timeout, cwd=root)
    if not findings.check("scorer.refresh_replay_runs", refresh.returncode == 0 and refresh_report.exists(), refresh.stderr[-400:]):
        return

    empty_research = workdir / "research-empty.json"
    empty_ux = workdir / "ux-empty.json"
    empty_research.write_text("{}", encoding="utf-8")
    empty_ux.write_text("{}", encoding="utf-8")
    score_output = workdir / "score.json"
    base_command = [
        python, str(root / "scripts" / "score_competition_v3.py"),
        "--profiles", str(profiles_path),
        "--external-report", str(eval_output),
        "--batch-report", str(run_result["report_path"]),
        "--refresh-report", str(refresh_report),
        "--research-report", str(empty_research),
        "--ux-report", str(empty_ux),
        "--output", str(score_output),
    ]
    scored = run(base_command, timeout=timeout, cwd=root)
    if not findings.check("scorer.proxy_runs", scored.returncode == 0 and score_output.exists(), scored.stderr[-400:]):
        return
    report = json.loads(score_output.read_text(encoding="utf-8"))
    findings.check("scorer.declares_missing_inputs",
                   {entry["report"] for entry in report["missing_inputs"]} >= {"research", "ux"} and report["is_proxy"] is True,
                   json.dumps(report["missing_inputs"]))
    findings.check("scorer.no_accuracy_claim", "No accuracy" in report["claim_boundary"] or "no accuracy" in report["claim_boundary"].lower(),
                   report["claim_boundary"])
    strict = run(base_command + ["--require-complete-inputs"], timeout=timeout, cwd=root)
    findings.check("scorer.strict_mode_fails", strict.returncode != 0, f"exit={strict.returncode}")


def check_prototype_gate(findings: Findings, python: str, root: Path, workdir: Path, run_result: dict[str, Any], timeout: int) -> None:
    """The laboratory HTML must render gated evidence only."""
    html_output = workdir / "prototype.html"
    completed = run([
        python, str(root / "scripts" / "build_prototype.py"),
        "--input", str(run_result["profiles_path"]),
        "--external-observations", str(root / OBSERVATIONS_VALID), str(root / OBSERVATIONS_ADVERSARIAL),
        "--output", str(html_output),
    ], timeout=timeout, cwd=root)
    if not findings.check("prototype.renders", completed.returncode == 0 and html_output.exists(), completed.stderr[-400:]):
        return
    html = html_output.read_text(encoding="utf-8")
    forbidden = ["unapproved-rights", "not-exact-entity", "missing-proof", "non-hex-hash", "unlisted-org", "dup-conflict"]
    findings.check("prototype.publishable_only", not [token for token in forbidden if token in html],
                   f"prototype rendered non-publishable observations: {[token for token in forbidden if token in html]}")
    findings.check("prototype.gate_diagnostics_reported", "accepted" in completed.stdout and "rejected" in completed.stdout,
                   completed.stdout[-200:])


def negative_control(findings: Findings, python: str, root: Path, workdir: Path, timeout: int) -> None:
    """Deliberately alter an expected result and require the runner's checks to fail.

    Two independent controls:
    (a) an envelope tampered in memory (external block moved to another organisation) must be
        rejected by ``check_cross_org_isolation``;
    (b) a CLI run whose observation file is empty must fail ``aggregation.per_org_matches_total``
        style expectations (no accepted observations) while the CLI itself still exits 0.
    """
    empty_file = workdir / "empty-observations.jsonl"
    empty_file.write_text("", encoding="utf-8")
    empty_run = run_batch(python, root, workdir, label="negative-control", observations=[empty_file], timeout=timeout)
    findings.check("negative_control.cli_still_exits_zero", empty_run["returncode"] == 0,
                   f"control run exit={empty_run['returncode']}: {empty_run['stderr'][-300:]}")
    findings.check("negative_control.altered_expectation_fails",
                   acceptance_expectation(empty_run["report"]) is False,
                   "the runner's acceptance expectation unexpectedly held for an empty observation file")

    isolation = Findings(quiet=True)
    tampered_envelope = {
        "organisation_number": "923609016",
        "external": {"accepted_observation_ids": ["o1"], "publishable_only": True, "organisation_number": "923609016"},
    }
    check_cross_org_isolation(isolation, {"envelopes": [tampered_envelope]}, {"o1": {"987654321"}})
    detected = bool(isolation.failures)

    contract_probe = Findings(quiet=True)
    tampered_valid = copy.deepcopy(empty_run["envelopes"][0]) if empty_run["envelopes"] else {}
    # A moved external block must be rejected by the contract validator as well.
    if tampered_valid:
        tampered_valid.setdefault("external", {"accepted_observation_ids": ["o1"], "publishable_only": True, "organisation_number": "000000000"})
        contract_probe.check("probe.external_org_mismatch", bool(validate_contract_envelope(tampered_valid)["errors"]),
                             "validator accepted an external block from another organisation")

    findings.check("negative_control.tampered_envelope_detected", detected and bool(contract_probe.failures) is False,
                   f"isolation check detected={detected}, contract probe errors={contract_probe.entries}")
    if not detected:
        raise SystemExit(3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=None, help="Project root (default: resolved from this script)")
    parser.add_argument("--python", default=sys.executable, help="Interpreter used for subprocesses")
    parser.add_argument("--workdir", default=None, help="Work directory (default: a fresh temporary directory)")
    parser.add_argument("--timeout", type=int, default=120, help="Per-subprocess timeout in seconds")
    parser.add_argument("--json-report", default=None, help="Optional path for a machine-readable check summary")
    parser.add_argument("--keep-workdir", action="store_true", help="Do not delete the temporary work directory")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else ROOT
    required = [root / ORGS_FIXTURE, root / BULK_FIXTURE, root / OBSERVATIONS_VALID, root / OBSERVATIONS_ADVERSARIAL,
                root / AUDIT_LABELS, root / "scripts" / "run_competition_batch.py"]
    missing = [str(path) for path in required if not path.exists()]
    temporary = args.workdir is None
    workdir = Path(tempfile.mkdtemp(prefix="signalpost-e2e-")) if temporary else Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    print(f"project root : {root}")
    print(f"work directory: {workdir}")
    findings = Findings()
    try:
        if not findings.check("environment.fixtures_present", not missing, f"missing: {missing}"):
            raise SystemExit(2)

        first = run_batch(args.python, root, workdir, label="valid", observations=[root / OBSERVATIONS_VALID, root / OBSERVATIONS_ADVERSARIAL], timeout=args.timeout)
        check_contract(findings, first)
        orgs_by_observation = {
            "places-summary-923609016": {"923609016"}, "site-post-923609016": {"923609016"},
            "shared-1": {"923609016", "987654321"}, "news-mention-987654321": {"987654321"},
            "stale-job": {"987654321"}, "dup-identical": {"987654321"},
        }
        by_org = check_acceptance_and_rejection(findings, first)
        check_duplicates_and_freshness(findings, first, by_org)
        check_aggregation(findings, first)
        check_separation(findings, first)
        check_cross_org_isolation(findings, first, orgs_by_observation)
        check_contract_api_negative_cases(findings, first)
        check_prototype_gate(findings, args.python, root, workdir, first, args.timeout)
        check_scorer(findings, args.python, root, workdir, first, args.timeout)
        check_malformed_record_tolerance(findings, args.python, root, workdir, args.timeout)
        check_resume_external_policy(findings, args.python, root, workdir, args.timeout)

        second = run_batch(args.python, root, workdir, label="repeat", observations=[root / OBSERVATIONS_VALID, root / OBSERVATIONS_ADVERSARIAL], timeout=args.timeout)
        findings.check("determinism.repeat_run_succeeds", second["returncode"] == 0, second["stderr"][-400:])
        check_determinism(findings, first, second)
        negative_control(findings, args.python, root, workdir, args.timeout)
        offline = run_batch(args.python, root, workdir, label="official-only", observations=[], timeout=args.timeout)
        findings.check("official_only.mode_unchanged",
                       offline["returncode"] == 0
                       and "external" not in offline["report"]
                       and all("external" not in envelope for envelope in offline["envelopes"]),
                       "omitting --external-observations must produce no external block and no external report section")
    except subprocess.TimeoutExpired as exc:
        findings.check("runner.subprocess_timeout", False, f"{exc.cmd} exceeded {args.timeout}s")
    finally:
        print()
        print(f"checks: {len(findings.entries)} | failures: {len(findings.failures)} | critical: {len(findings.critical_failures)}")
        for entry in findings.failures:
            print(f"  FAILED [{entry['severity']}] {entry['check']}: {entry['detail']}")
        if args.json_report:
            Path(args.json_report).write_text(json.dumps({"checks": findings.entries}, indent=2) + "\n", encoding="utf-8")
        if temporary and not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    raise SystemExit(1 if findings.critical_failures else 0)


if __name__ == "__main__":
    main()
