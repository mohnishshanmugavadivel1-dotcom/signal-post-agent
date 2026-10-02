from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .external_footprint import is_organisation_number, parse_timestamp
from .official import accounting_obligation_assessment
from .sampling import iter_bulk


TERMINAL_STATES = {
    "complete",
    "not_applicable",
    "not_found",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
}

#: Version of the documented contract (`OUTPUT_CONTRACT.md`) this serialiser implements.
CONTRACT_VERSION = "signalpost-minimal-v1"

#: Availability vocabulary documented in OUTPUT_CONTRACT.md. The internal evidence vocabulary is
#: richer (see `EVIDENCE_STATUS_TO_AVAILABILITY`), so the mapping below is deliberately explicit and
#: is also recorded at runtime under `run_report.contract_status.mapping` by the batch CLI.
AVAILABILITY_STATES = {
    "available",
    "not_available",
    "blocked",
    "not_applicable",
    "ambiguous",
    "failed",
}

#: Documented availability states have no `not_fetched` member, so a module that was requested but
#: never attempted maps to `not_available`. The precise internal state is preserved in `modules`.
EVIDENCE_STATUS_TO_AVAILABILITY = {
    "available": "available",
    "not_found": "not_available",
    "not_applicable": "not_applicable",
    "not_fetched": "not_available",
    "blocked": "blocked",
    "source_error": "failed",
}

#: OUTPUT_CONTRACT.md shows `run.terminal_status: "completed"` but does not enumerate the allowed
#: values. This implementation emits exactly two, derived from the entity state.
CONTRACT_TERMINAL_STATUS = {"complete": "completed", "submission_error": "failed"}

CONTRACT_REQUIRED_TOP_LEVEL = (
    "contract_version",
    "organisation_number",
    "run",
    "claims",
    "evidence",
    "changes",
    "errors",
    "operations",
    "state",
    "modules",
)

CONTRACT_REQUIRED_EVIDENCE_FIELDS = ("id", "source_url", "source_class", "retrieved_at", "content_sha256", "claim_span")

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def availability_state(record: dict[str, Any] | None) -> str:
    """Map an internal evidence record to the documented availability vocabulary."""
    if not record:
        return "not_available"
    return EVIDENCE_STATUS_TO_AVAILABILITY.get(str(record.get("status") or ""), "failed")


def _claim_span(module: str, record: dict[str, Any], profile: dict[str, Any]) -> str:
    """Human-readable support string for one evidence record.

    `OUTPUT_CONTRACT.md` documents `claim_span` as an excerpt supporting the claim. Official API
    responses are JSON documents rather than prose, so for those this implementation emits a
    deterministic summary of what the source returned (never an invented quotation).
    """
    note = str(record.get("note") or "").strip()
    if note:
        return note[:500]
    value = record.get("value")
    if module in {"registry", "registry_live"} and isinstance(value, dict):
        name = value.get("navn") or profile.get("name") or ""
        org = profile.get("organisation_number") or ""
        return f"{name}, organisation number {org}".strip(", ")[:500]
    if isinstance(value, dict) and isinstance(value.get("records"), list):
        return f"{len(value['records'])} annual-account record(s) returned"[:500]
    for key, label in (("roles", "public role record(s)"), ("locations", "registered subunit(s)")):
        if isinstance(value, dict) and isinstance(value.get(key), list):
            return f"{len(value[key])} {label} returned"[:500]
    if record.get("source_url"):
        return f"Source response received from {record['source_url']}"[:500]
    return f"{module} evidence record"[:500]


def build_claims_and_evidence(
    profile: dict[str, Any],
    modules: Iterable[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the documented `claims` and `evidence` arrays from official evidence only.

    One evidence record produces exactly one claim, so `claims` and `evidence` always have the
    same length and every `evidence_ids` entry resolves inside the same envelope. External
    observations never enter this function; they travel in the separate `external` block.
    """
    claims: list[dict[str, Any]] = []
    evidence_records: list[dict[str, Any]] = []
    records = profile.get("evidence", {})
    for module in modules:
        record = records.get(module)
        availability = availability_state(record)
        evidence_id = f"ev-{module}"
        evidence_records.append({
            "id": evidence_id,
            "source_url": (record or {}).get("source_url"),
            "source_class": (record or {}).get("source_class") or (record or {}).get("source_type"),
            "retrieved_at": (record or {}).get("retrieved_at"),
            "content_sha256": (record or {}).get("content_sha256"),
            "claim_span": _claim_span(module, record or {}, profile),
        })
        claims.append({
            "field": module,
            # `confidence` here is a deterministic presence indicator (1.0 when the source returned
            # a value, 0.0 otherwise). It is NOT a calibrated probability, and no uncertainty model
            # was calibrated for it.
            "value": (record or {}).get("value") if availability == "available" else None,
            "availability": availability,
            "confidence": 1.0 if availability == "available" else 0.0,
            "evidence_ids": [evidence_id],
        })
    return claims, evidence_records


def build_errors(profile: dict[str, Any], modules: Iterable[str]) -> list[dict[str, Any]]:
    """Blocking and failed modules for this envelope, with the internal state preserved."""
    errors: list[dict[str, Any]] = []
    records = profile.get("evidence", {})
    for module in modules:
        record = records.get(module)
        availability = availability_state(record)
        if availability in {"available", "not_available", "not_applicable"}:
            continue
        errors.append({
            "module": module,
            "availability": availability,
            "state": evidence_terminal_state(record),
            "note": (record or {}).get("note"),
            "source_url": (record or {}).get("source_url"),
        })
    return errors


def contract_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
    external_block: dict[str, Any] | None = None,
    operations: dict[str, Any] | None = None,
    changes: list[dict[str, Any]] | None = None,
    third_party_cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Serialise one organisation into the documented output contract.

    The envelope is a superset of the historical `terminal_envelope` output: the legacy keys
    (`run_id`, `state`, `started_at`, `completed_at`, `modules`, `profile`) are retained so existing
    consumers keep working, and the documented contract keys (`contract_version`, `run`, `claims`,
    `evidence`, `changes`, `errors`, `operations`) are added.

    Official and external evidence stay separate: `claims`/`evidence` derive only from
    `profile.evidence`, while external observations travel in the `external` block, which is always
    publishable-only. `changes` is empty for a single batch run; the snapshot-diff path is
    `scripts/run_refresh_replay.py`.
    """
    module_list = list(modules)
    module_states = {
        module: {
            "state": evidence_terminal_state(profile.get("evidence", {}).get(module)),
            "retry_count": int((profile.get("evidence", {}).get(module) or {}).get("retry_count") or 0),
            "final_timestamp": (profile.get("evidence", {}).get(module) or {}).get("retrieved_at") or completed_at,
        }
        for module in module_list
    }
    entity_state = "submission_error" if any(item["state"] == "submission_error" for item in module_states.values()) else "complete"
    claims, evidence_records = build_claims_and_evidence(profile, module_list)
    run_metrics = profile.get("run_metrics") or {}
    operations_block = {
        "requests": int(run_metrics.get("requests") or 0),
        "runtime_ms": int(run_metrics.get("runtime_ms") or 0),
        "third_party_cost_usd": float(third_party_cost_usd),
    }
    if operations:
        operations_block.update(operations)
    envelope: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "organisation_number": profile["organisation_number"],
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": CONTRACT_TERMINAL_STATUS.get(entity_state, "failed"),
        },
        "claims": claims,
        "evidence": evidence_records,
        "changes": list(changes or []),
        "errors": build_errors(profile, module_list),
        "operations": operations_block,
        # Legacy fields retained for existing consumers and for the precise internal vocabulary.
        "run_id": run_id,
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "modules": module_states,
        "profile": profile,
    }
    if external_block is not None:
        envelope["external"] = external_block
    return envelope


def _external_block_errors(external: dict[str, Any], org: str) -> list[str]:
    """Cross-check an external block's identifiers against its aggregate counters.

    The block is a contract extension, but its counts are published evidence: a count that
    contradicts the identifier list it summarises is a defect, not a formatting choice. The checks
    follow the aggregation policy documented in :func:`external_footprint.aggregate_footprint` —
    ``accepted_observations`` is the number of accepted records, ``accepted = fresh + stale`` and
    ``counted_observations`` follows ``counts_scope`` — and the block produced by
    :func:`external_pipeline.company_external_block`, which always carries ``footprint``.

    Duplicate identifiers are rejected explicitly so that repeating an id cannot inflate a count.
    An empty, self-consistent block stays valid.
    """
    errors: list[str] = []
    if str(external.get("organisation_number")) != str(org):
        errors.append("external block organisation_number does not match the envelope")
    if external.get("publishable_only") is not True:
        errors.append("external block must be publishable-only")

    def identifier_list(name: str) -> list[str]:
        value = external.get(name, [])
        if not isinstance(value, list):
            errors.append(f"external {name} must be a list")
            return []
        collected: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                errors.append(f"external {name} entries must be non-empty strings")
            else:
                collected.append(item)
        if len(collected) != len(set(collected)):
            errors.append(f"external {name} must not repeat an identifier")
        return collected

    accepted_ids = identifier_list("accepted_observation_ids")
    stale_ids = identifier_list("stale_observation_ids")
    if not set(stale_ids) <= set(accepted_ids):
        errors.append("external stale observation ids must also be accepted observation ids")

    rejected = external.get("rejected_observations", [])
    rejected_ids: list[str] = []
    if not isinstance(rejected, list):
        errors.append("external rejected_observations must be a list")
        rejected = []
    for entry in rejected:
        if not isinstance(entry, dict):
            errors.append("rejected observations must be objects")
            continue
        if set(entry) - {"id", "reasons"}:
            errors.append("rejected observations may only carry id and reasons")
        value = entry.get("id")
        # `None` is a legitimate diagnostic id (the record had none) and a repeated id is
        # legitimate too: one stub is emitted per rejected record, and conflicting duplicates
        # share an id by definition. What is not legitimate is a non-string, non-null id, which
        # a consumer cannot use as a key.
        if value is not None and (not isinstance(value, str) or not value.strip()):
            errors.append("rejected observation ids must be non-empty strings or null")
        elif value is not None:
            rejected_ids.append(value)
    both = sorted(set(rejected_ids) & set(accepted_ids))
    if both:
        errors.append(f"observations cannot be both accepted and rejected: {both}")

    footprint = external.get("footprint")
    if not isinstance(footprint, dict):
        errors.append("external block must carry a footprint object with its aggregate counts")
        return errors

    def count(name: str) -> int | None:
        value = footprint.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"external footprint {name} must be a non-negative integer")
            return None
        return value

    accepted_count = count("accepted_observations")
    fresh_count = count("fresh_observations")
    stale_count = count("stale_observations")
    rejected_count = count("rejected_observations")
    counted = count("counted_observations")

    if accepted_count is not None and accepted_count != len(accepted_ids):
        errors.append(
            "external footprint accepted_observations "
            f"({accepted_count}) does not match the accepted observation ids ({len(accepted_ids)})"
        )
    if stale_count is not None and stale_count != len(stale_ids):
        errors.append(
            "external footprint stale_observations "
            f"({stale_count}) does not match the stale observation ids ({len(stale_ids)})"
        )
    if rejected_count is not None and rejected_count != len(rejected):
        errors.append(
            "external footprint rejected_observations "
            f"({rejected_count}) does not match the rejection diagnostics ({len(rejected)})"
        )
    rejections = footprint.get("rejections")
    if not isinstance(rejections, list):
        errors.append("external footprint rejections must be a list")
    elif rejected_count is not None and len(rejections) != rejected_count:
        errors.append("external footprint rejections must match rejected_observations")
    if accepted_count is not None and fresh_count is not None and stale_count is not None:
        if accepted_count != fresh_count + stale_count:
            errors.append("external footprint accepted_observations must equal fresh + stale")
    scope = footprint.get("counts_scope")
    if scope not in {"accepted", "fresh"}:
        errors.append("external footprint counts_scope must be 'accepted' or 'fresh'")
    elif counted is not None and accepted_count is not None and fresh_count is not None:
        expected = fresh_count if scope == "fresh" else accepted_count
        if counted != expected:
            errors.append(f"external footprint counted_observations must equal {expected} for counts_scope {scope!r}")
    block_scope = external.get("counts_scope")
    if block_scope is not None and block_scope != scope:
        errors.append("external block counts_scope does not match its footprint")
    status = footprint.get("status")
    if accepted_count is not None:
        if status not in {"available", "not_available"}:
            errors.append("external footprint status must be available or not_available")
        elif (status == "available") != (accepted_count > 0):
            errors.append("external footprint status must be available exactly when observations were accepted")
    return errors


def validate_contract_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Validate one envelope against the documented contract and the internal invariants.

    Returns ``{"passed": bool, "errors": [...], "checks": {...}}``. Checks are pure functions of the
    envelope, so the same envelope always validates identically.
    """
    errors: list[str] = []

    for key in CONTRACT_REQUIRED_TOP_LEVEL:
        if key not in envelope:
            errors.append(f"missing required field: {key}")
    if errors:
        return {"passed": False, "errors": errors, "checks": {"required_fields": False}}

    org = envelope["organisation_number"]
    if not is_organisation_number(org):
        errors.append("organisation_number must be exactly nine ASCII digits")

    run = envelope["run"]
    if not isinstance(run, dict):
        errors.append("run must be an object")
    else:
        for key in ("run_id", "started_at", "completed_at", "terminal_status"):
            if key not in run:
                errors.append(f"run is missing {key}")
        if not isinstance(run.get("run_id"), str) or not str(run.get("run_id") or "").strip():
            errors.append("run.run_id must be a non-empty string")
        for key in ("started_at", "completed_at"):
            value = run.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"run.{key} must be a non-empty string")
            elif parse_timestamp(value) is None:
                errors.append(f"run.{key} must be an ISO-8601 timestamp")
        if run.get("terminal_status") not in CONTRACT_TERMINAL_STATUS.values():
            errors.append("run.terminal_status must be one of the documented implementation values")

    claims = envelope["claims"]
    evidence_records = envelope["evidence"]
    if not isinstance(claims, list):
        errors.append("claims must be a list")
    if not isinstance(evidence_records, list):
        errors.append("evidence must be a list")
    evidence_ids: list[str] = []
    if isinstance(evidence_records, list):
        for record in evidence_records:
            if not isinstance(record, dict):
                errors.append("evidence entries must be objects")
                continue
            for key in CONTRACT_REQUIRED_EVIDENCE_FIELDS:
                if key not in record:
                    errors.append(f"evidence entry missing {key}")
            # OUTPUT_CONTRACT.md documents `evidence[]` as "the documented fields only". An
            # allow-list (rather than a deny-list of suspicious names) is the only form of this
            # check that cannot be bypassed by renaming: internal fields must not travel into a
            # published envelope just because their key does not look internal.
            undocumented = sorted(set(record) - set(CONTRACT_REQUIRED_EVIDENCE_FIELDS))
            if undocumented:
                errors.append(f"evidence {record.get('id')} carries undocumented fields: {undocumented}")
            digest = record.get("content_sha256")
            if digest is not None and not _HASH_PATTERN.match(str(digest)):
                errors.append(f"evidence {record.get('id')} has a malformed content_sha256")
            if record.get("id") is not None:
                evidence_ids.append(str(record["id"]))
    if len(evidence_ids) != len(set(evidence_ids)):
        errors.append("evidence ids must be unique within an envelope")
    claim_labels: list[str] = []
    claims_typed = True
    if isinstance(claims, list):
        for index, claim in enumerate(claims, start=1):
            if not isinstance(claim, dict):
                errors.append("claim entries must be objects")
                claims_typed = False
                continue
            # The field is a display label, a mapping key and a sort key, so it is typed before any
            # of those uses. Untyped values used to raise TypeError out of this validator.
            field = claim.get("field")
            if not isinstance(field, str) or not field.strip():
                errors.append(f"claim #{index} field must be a non-empty string")
                claims_typed = False
                label = f"#{index}"
            else:
                label = field
                claim_labels.append(field)
            availability = claim.get("availability")
            if availability not in AVAILABILITY_STATES:
                errors.append(f"claim {label} has an unsupported availability state")
            if availability == "not_available" and claim.get("value") is not None:
                errors.append(f"claim {label} must not carry a value when availability is not_available")
            confidence = claim.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0.0 <= float(confidence) <= 1.0:
                errors.append(f"claim {label} has an invalid confidence")
            ids = claim.get("evidence_ids")
            if not isinstance(ids, list) or not ids:
                errors.append(f"claim {label} must reference at least one evidence id")
            else:
                for evidence_id in ids:
                    if str(evidence_id) not in evidence_ids:
                        errors.append(f"claim {label} references unknown evidence {evidence_id}")

    operations = envelope["operations"]
    if not isinstance(operations, dict):
        errors.append("operations must be an object")
    else:
        for key in ("requests", "runtime_ms", "third_party_cost_usd"):
            if key not in operations:
                errors.append(f"operations is missing {key}")
        requests = operations.get("requests")
        if not isinstance(requests, int) or isinstance(requests, bool) or requests < 0:
            errors.append("operations.requests must be a non-negative integer")
        runtime = operations.get("runtime_ms")
        if not isinstance(runtime, int) or isinstance(runtime, bool) or runtime < 0:
            errors.append("operations.runtime_ms must be a non-negative integer")
        cost = operations.get("third_party_cost_usd")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or float(cost) < 0:
            errors.append("operations.third_party_cost_usd must be a non-negative number")

    for key in ("changes", "errors", "modules"):
        if not isinstance(envelope.get(key), (list, dict)):
            errors.append(f"{key} must be a list or object")

    # Internal invariants: one claim per evidence record, same module set, and the run metrics
    # reported by the CLI must match what the profile actually used.
    if isinstance(claims, list) and isinstance(evidence_records, list) and len(claims) != len(evidence_records):
        errors.append("claims and evidence must contain the same number of records")
    if isinstance(envelope.get("modules"), dict) and isinstance(claims, list):
        module_keys = list(envelope["modules"].keys())
        if not all(isinstance(key, str) for key in module_keys):
            errors.append("module names must be strings")
        elif claims_typed and sorted(claim_labels) != sorted(module_keys):
            # Only compared when every claim carries a valid string field; otherwise the field
            # error above is the actionable diagnostic and this comparison would be noise.
            errors.append("claims must cover exactly the requested modules")
    profile = envelope.get("profile")
    if isinstance(profile, dict):
        run_metrics = profile.get("run_metrics") or {}
        if isinstance(operations, dict) and "requests" in run_metrics:
            if int(run_metrics.get("requests") or 0) != int(operations.get("requests") or 0):
                errors.append("operations.requests does not match profile.run_metrics.requests")
        evidence_block = profile.get("evidence")
        if isinstance(evidence_block, dict):
            leaked = sorted(set(evidence_block) & {"external", "external_footprint", "observations"})
            if leaked:
                errors.append(f"external data must not live inside profile.evidence: {leaked}")
        # A stored external block is only legitimate when the envelope publishes a matching one.
        # This catches stale evidence surviving a resume and any profile/envelope disagreement.
        stored_block = profile.get("external_footprint")
        if stored_block is not None:
            top_block = envelope.get("external")
            if not isinstance(top_block, dict):
                errors.append("profile.external_footprint is present but the envelope has no external block")
            else:
                if str(stored_block.get("organisation_number")) != str(org):
                    errors.append("profile.external_footprint organisation_number does not match the envelope")
                stored_ids = sorted(str(item) for item in (stored_block.get("accepted_observation_ids") or []))
                top_ids = sorted(str(item) for item in (top_block.get("accepted_observation_ids") or []))
                if stored_ids != top_ids:
                    errors.append("profile.external_footprint does not match the envelope external block")

    external = envelope.get("external")
    if external is not None:
        if not isinstance(external, dict):
            errors.append("external must be an object when present")
        else:
            errors.extend(_external_block_errors(external, str(org)))

    checks = {
        "required_fields": True,
        "terminal_status_documented": True,
        "claims_resolve_to_evidence": not any("references unknown evidence" in item for item in errors),
        "operations_consistent": not any("operations." in item for item in errors),
        "external_separated": not any("external" in item for item in errors),
        "counts_consistent": not any("does not match" in item or "must equal" in item for item in errors),
    }
    return {"passed": not errors, "errors": errors, "checks": checks}


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
    external_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backwards-compatible wrapper around :func:`contract_envelope`.

    Existing callers keep the historical keyword arguments; they now receive the documented
    contract keys as well.
    """
    return contract_envelope(
        profile,
        run_id=run_id,
        modules=modules,
        started_at=started_at,
        completed_at=completed_at,
        external_block=external_block,
    )


def read_organisation_inputs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    values: list[Any]
    if source.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers", [])
    elif source.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]
    records = []
    for value in values:
        org = value.get("organisation_number") if isinstance(value, dict) else value
        org = "".join(character for character in str(org or "") if character.isdigit())
        if len(org) != 9:
            raise ValueError(f"Invalid Norwegian organisation number: {value!r}")
        record = {"organisation_number": org}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    orgs = [record["organisation_number"] for record in records]
    if len(orgs) != len(set(orgs)):
        raise ValueError("Organisation-number input contains duplicates")
    return records


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def profiles_from_bulk(path: str | Path, organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested = list(organisation_numbers)
    wanted = set(requested)
    snapshot_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    for profile in iter_bulk(path):
        scanned += 1
        org = profile["organisation_number"]
        if org not in wanted:
            continue
        raw = profile.pop("raw", {})
        profile["evidence"] = {
            "registry": evidence(
                "registry",
                "available",
                "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value=raw,
                retrieved_at=retrieved_at,
                content_sha256=snapshot_sha256,
                source_row_key=org,
            ),
            "accounting_obligation": accounting_obligation_assessment(profile),
        }
        found[org] = profile
        if len(found) == len(wanted):
            break
    missing = [org for org in requested if org not in found]
    if missing:
        raise ValueError(f"Organisation numbers absent from registry snapshot: {missing[:10]}")
    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(found),
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "submission_error"
    status = record.get("status")
    if status == "available":
        return "complete"
    if status == "not_applicable":
        return "not_applicable"
    if status == "not_found":
        return "not_found"
    if status == "blocked":
        note = str(record.get("note") or "").casefold()
        return "blocked_robots" if "robot" in note else "blocked_policy"
    if status == "source_error":
        return "source_error"
    return "submission_error"


def validate_envelopes(envelopes: list[dict[str, Any]], expected_count: int) -> dict[str, Any]:
    orgs = [item.get("organisation_number") for item in envelopes]
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "state": state.get("state")}
        for item in envelopes
        for state in item.get("modules", {}).values()
        if state.get("state") not in TERMINAL_STATES
    ]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
    }
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": invalid_states}


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    return all(module in records and records[module].get("status") != "not_fetched" for module in modules)
