"""External-observation intake, hardening gates and profile integration.

This module is the single boundary between raw external-observation JSONL produced by
connector scripts and the competition batch pipeline. It is deliberately separate from
``external_footprint`` (which owns the publication policy and the aggregation math) and
from ``batch`` (which owns the official evidence contract):

* **intake**  — parse JSONL, keep malformed lines as diagnostics instead of aborting;
* **identity** — collapse identical duplicates, refuse conflicting duplicates, bind every
  record to the organisation number that requested it;
* **policy**  — delegate the publication decision to ``external_footprint.validate_observation``;
* **freshness** — separate acceptance from freshness so stale records can be reported,
  counted, or excluded explicitly;
* **integration** — attach a per-organisation external block to profiles and envelopes
  without mixing external observations into official evidence.

Observation file schema (JSONL, one object per line) — field names are fixed by the
connector scripts that already exist in this repository:

    id                     str   required, unique per organisation
    organisation_number    str   required, 9 digits
    platform               str   required, one of external_footprint.PLATFORMS
    signal_type            str   required, one of external_footprint.SIGNAL_TYPES
    source_url             str   required, absolute URL with a hostname
    retrieved_at           str   required, ISO-8601
    content_sha256         str   required, 64 lowercase hex characters
    exact_entity           bool  required, must be true
    identity_proof         list  required, non-empty
    acquisition_mode       str   required, one of PUBLISHABLE_ACQUISITION_MODES for publication
    rights_status          str   required, must be "approved" for publication
    source_class           str   recommended
    evidence_span          str   required for review / public_post / public_mention
    sentiment_label        str   optional, one of positive/neutral/negative/mixed
    sentiment_model_version str  required when sentiment_label is present
    reviewer_id            str   optional, enables the reviewer-based sentiment gate
    metrics                dict  optional, source-specific counters

Hash verification limits (important, do not overstate): the intake layer validates hash
*syntax* (64 lowercase hex characters) and record-level *content* duplicates via
``observation_fingerprint``. It cannot recompute a content digest because observations do
not carry the fetched bytes — the raw capture stays with the connector that fetched it.
Any record whose hash cannot be independently recomputed is therefore accepted only on
syntax, and the run report records that limitation in ``external.hash_verification``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .external_footprint import (
    EXPERIMENTAL_ACQUISITION_MODES,
    PUBLISHABLE_ACQUISITION_MODES,
    _is_organisation_number,
    aggregate_footprint,
    diagnostic_id,
    observation_fingerprint,
    parse_timestamp,
    publishable_observation,
    validate_observation,
)


OBSERVATION_SCHEMA_VERSION = 1
GATE_POLICY_VERSION = "signalpost_external_gate_v1"

#: Hash handling is syntax-only because observation records do not carry source bytes.
HASH_VERIFICATION = {
    "mode": "syntax_and_content_duplicate",
    "syntax": "64 lowercase hexadecimal characters",
    "recomputed_from_bytes": False,
    "limitation": (
        "Observation records do not carry the fetched bytes, so the gate cannot recompute the "
        "content digest. Connectors compute the digest over the bytes they fetched; the gate "
        "verifies syntax and detects identical payload duplicates only."
    ),
}


class ObservationInputError(ValueError):
    """Raised when an observation file cannot be read at all (missing/unreadable path)."""


def read_observation_file(path: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read one observation JSONL file.

    Returns ``(records, malformed)``. Malformed lines are reported, never silently dropped and
    never allowed to abort the whole batch. A missing or unreadable file raises
    :class:`ObservationInputError` because that is an operator error, not a data-quality signal.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # A binary or wrongly-encoded file is an operator error like a missing file: report it in
        # the same structured form instead of letting a codec traceback escape the CLI.
        raise ObservationInputError(
            f"Cannot read external observation file {source}: not valid UTF-8 ({exc})"
        ) from exc
    except OSError as exc:
        raise ObservationInputError(f"Cannot read external observation file {source}: {exc}") from exc
    records: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            malformed.append({"file": str(source), "line": number, "error": f"invalid JSON: {exc.msg}"})
            continue
        if not isinstance(value, dict):
            malformed.append({"file": str(source), "line": number, "error": f"record is {type(value).__name__}, expected object"})
            continue
        records.append({**value, "__source_file": str(source), "__source_line": number})
    return records, malformed


def _strip_provenance(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("__source_")}


def _identity_key(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("organisation_number") or ""), str(item.get("id") or ""))


def deduplicate_observations(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collapse identical duplicates and refuse conflicting duplicates.

    Policy (explicit, and the reason for it):

    * identity is scoped to ``(organisation_number, id)`` — the same id may legitimately exist
      for two different organisations, and must not cause either record to be dropped;
    * records that share an identity **and** a payload fingerprint collapse to one occurrence
      (deterministically: the first occurrence in file order is kept);
    * records that share an identity but differ in payload are *conflicting*: the whole group is
      withheld from publication and reported, because the gate cannot know which version is real;
    * identical payloads that carry *different* ids are reported as content duplicates but are not
      collapsed, because distinct ids may be legitimate distinct captures (for example a metric
      refresh), and silently dropping either would lose data.

    Returns ``(unique_records, report)``. Provenance fields (``__source_file``/``__source_line``)
    are preserved on the returned records for diagnostics.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in records:
        groups.setdefault(_identity_key(item), []).append(item)

    unique: list[dict[str, Any]] = []
    collapsed = 0
    conflicts: list[dict[str, Any]] = []
    conflicted_records = 0
    ids_by_org: dict[str, set[str]] = {}

    for (org, observation_id), items in groups.items():
        fingerprints = {observation_fingerprint(_strip_provenance(item)) for item in items}
        if len(fingerprints) > 1:
            conflicted_records += len(items)
            conflicts.append({
                "organisation_number": org,
                "id": observation_id,
                "occurrences": len(items),
                "distinct_payloads": len(fingerprints),
                "source_lines": [item.get("__source_line") for item in items],
            })
            continue
        unique.append(items[0])
        collapsed += len(items) - 1
        ids_by_org.setdefault(org, set()).add(observation_id)

    shared_ids = sorted(
        observation_id
        for observation_id, orgs in _invert(ids_by_org).items()
        if len(orgs) > 1
    )
    content_duplicates = _content_duplicate_ids(unique, conflicted={conflict["id"] for conflict in conflicts})

    return unique, {
        "policy_version": GATE_POLICY_VERSION,
        "identity_scope": "organisation_number + id",
        "input_records": len(records),
        "unique_records": len(unique),
        "collapsed_identical": collapsed,
        "conflicting_groups": conflicts,
        "conflicted_records": conflicted_records,
        "ids_shared_across_organisations": shared_ids,
        "content_duplicate_ids": content_duplicates,
    }


def _invert(ids_by_org: dict[str, set[str]]) -> dict[str, set[str]]:
    inverted: dict[str, set[str]] = {}
    for org, ids in ids_by_org.items():
        for observation_id in ids:
            inverted.setdefault(observation_id, set()).add(org)
    return inverted


def content_key(item: dict[str, Any]) -> str:
    """Fingerprint of an observation payload ignoring the observation id.

    Two records that describe the same event but carry different ids produce the same content key;
    they are reported as content duplicates so an operator can see the repeated evidence.
    """
    return observation_fingerprint({key: value for key, value in _strip_provenance(item).items() if key != "id"})


def _content_duplicate_ids(records: list[dict[str, Any]], *, conflicted: set[str]) -> list[str]:
    by_fingerprint: dict[str, set[str]] = {}
    for item in records:
        if str(item.get("id") or "") in conflicted:
            continue
        by_fingerprint.setdefault(content_key(item), set()).add(str(item.get("id") or ""))
    duplicates: set[str] = set()
    for ids in by_fingerprint.values():
        if len(ids) > 1:
            duplicates.update(ids)
    return sorted(duplicates)


def gate_observations(
    records: list[dict[str, Any]],
    *,
    organisation_numbers: Iterable[str],
    malformed: list[dict[str, Any]] | None = None,
    as_of: str | None = None,
    freshness_days: int = 45,
    enforce_freshness: bool = False,
) -> dict[str, Any]:
    """Apply identity, rights and freshness policy to one observation file.

    Every record is placed in exactly one of three buckets, and every non-accepted record keeps
    a machine-readable reason list: ``accepted``, ``rejected`` (failed the publication policy),
    or ``unmatched`` (the organisation number is not part of the requested batch). Records that
    lost a duplicate conflict are rejected with the reason ``"conflicting duplicate observations
    for the same organisation and id"`` so the operator can see them without opening the file.
    """
    requested = {str(org) for org in organisation_numbers}
    unique, duplicates = deduplicate_observations(records)
    conflicted_keys = {
        (str(conflict["organisation_number"]), str(conflict["id"])) for conflict in duplicates["conflicting_groups"]
    }

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    for item in unique:
        org_value = item.get("organisation_number")
        org = org_value if isinstance(org_value, str) else ""
        # A malformed identifier is a data-quality rejection, not an out-of-batch record: it must
        # appear in the rejection reasons, not in the unmatched bucket.
        if not _is_organisation_number(org_value):
            rejected.append({
                "id": item.get("id"),
                "organisation_number": org or None,
                "source_line": item.get("__source_line"),
                "reasons": validate_observation(item),
            })
            continue
        if org not in requested:
            unmatched.append({
                "id": item.get("id"),
                "organisation_number": org,
                "source_line": item.get("__source_line"),
                "reasons": ["organisation number is not part of this batch"],
            })
            continue
        reasons = validate_observation(item, organisation_number=org)
        if reasons:
            rejected.append({
                "id": item.get("id"),
                "organisation_number": org,
                "source_line": item.get("__source_line"),
                "reasons": reasons,
            })
            continue
        accepted.append(item)

    for conflict in duplicates["conflicting_groups"]:
        for line in conflict["source_lines"]:
            rejected.append({
                "id": conflict["id"],
                "organisation_number": conflict["organisation_number"],
                "source_line": line,
                "reasons": ["conflicting duplicate observations for the same organisation and id"],
            })
    # A conflicting group must never stay accepted.
    accepted = [
        item
        for item in accepted
        if (str(item.get("organisation_number") or ""), str(item.get("id") or "")) not in conflicted_keys
    ]

    footprint = aggregate_footprint(
        accepted,
        as_of=as_of,
        freshness_days=freshness_days,
        enforce_freshness=enforce_freshness,
    )
    by_organisation: dict[str, dict[str, Any]] = {}
    for org in sorted(requested):
        org_records = [item for item in accepted if str(item.get("organisation_number")) == org]
        org_accepted = [
            {"id": item.get("id"), "platform": item.get("platform"), "signal_type": item.get("signal_type")}
            for item in org_records
        ]
        org_rejected = [
            {"id": diagnostic_id(entry["id"]), "reasons": entry["reasons"]}
            for entry in rejected
            if entry["organisation_number"] == org
        ]
        org_footprint = aggregate_footprint(
            org_records,
            as_of=as_of,
            freshness_days=freshness_days,
            enforce_freshness=enforce_freshness,
        )
        by_organisation[org] = {
            "accepted_ids": sorted(str(item.get("id")) for item in org_records),
            "rejected": org_rejected,
            "accepted_observations": len(org_records),
            "fresh_observations": org_footprint["fresh_observations"],
            "stale_observations": org_footprint["stale_observations"],
            "status": org_footprint["status"],
            "strictly_publishable": bool(org_records),
        }

    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "policy_version": GATE_POLICY_VERSION,
        "counts_scope": "fresh" if enforce_freshness else "accepted",
        "freshness_days": freshness_days,
        "as_of": footprint["freshness_policy"]["as_of"],
        "malformed": list(malformed or []),
        "duplicates": duplicates,
        "accepted": accepted,
        "rejected": rejected,
        "unmatched": unmatched,
        "by_organisation": by_organisation,
        "footprint": footprint,
        "hash_verification": HASH_VERIFICATION,
    }


def coverage_from_observations(
    observations: list[dict[str, Any]],
    *,
    organisation_numbers: Iterable[str],
    as_of: str | None = None,
    freshness_days: int = 45,
) -> dict[str, float]:
    """Per-signal coverage shares for a batch of organisations.

    Shared by the audit evaluator and the competition proxy so the two cannot report different
    coverage for the same artefacts. Only publishable observations for organisations in the batch
    count; anything else is ignored here and reported separately by the caller.
    """
    requested = [str(org) for org in organisation_numbers]
    in_batch = [item for item in observations if str(item.get("organisation_number")) in set(requested)]
    accepted = [item for item in in_batch if publishable_observation(item)]
    fresh = [item for item in accepted if _observation_is_fresh(item, as_of=as_of, freshness_days=freshness_days)]
    orgs_by_platform: dict[str, set[str]] = {}
    orgs_by_signal: dict[str, set[str]] = {}
    orgs_with_sentiment: set[str] = set()
    fresh_orgs: set[str] = set()
    for item in accepted:
        org = str(item.get("organisation_number"))
        orgs_by_platform.setdefault(str(item.get("platform")), set()).add(org)
        orgs_by_signal.setdefault(str(item.get("signal_type")), set()).add(org)
        if item.get("sentiment_label") is not None:
            orgs_with_sentiment.add(org)
    for item in fresh:
        fresh_orgs.add(str(item.get("organisation_number")))

    def share(selected: Iterable[str]) -> float:
        wanted = set(selected)
        return round(sum(1 for org in requested if org in wanted) / len(requested), 6) if requested else 0.0

    def orgs_for(categories: Iterable[str]) -> set[str]:
        return set().union(*(orgs_by_signal.get(category, set()) for category in categories)) if categories else set()

    return {
        "any_external": share({org for orgs in orgs_by_platform.values() for org in orgs}),
        "two_platforms": share({org for org in requested if sum(1 for orgs in orgs_by_platform.values() if org in orgs) >= 2}),
        "workforce_jobs": share(orgs_for(("job_posting", "workforce_snapshot"))),
        "ratings_reviews": share(orgs_for(("review", "review_summary", "place_summary"))),
        "buzz_engagement": share(orgs_for(("public_post", "public_mention", "profile_metrics"))),
        "sentiment": share(orgs_with_sentiment),
        "fresh": share(fresh_orgs),
    }


def _observation_is_fresh(item: dict[str, Any], *, as_of: str | None, freshness_days: int) -> bool:
    now = parse_timestamp(as_of)
    retrieved = parse_timestamp(item.get("retrieved_at"))
    if now is None or retrieved is None:
        return False
    delta = (now - retrieved).total_seconds()
    return 0 <= delta <= freshness_days * 86_400


AUDIT_IDENTITY_POLICY = "organisation_number + id"


def audit_identity(item: dict[str, Any]) -> tuple[str, str] | None:
    """Composite identity shared by observations, audit labels and coverage.

    Observation ids are unique per organisation, not globally (see
    :func:`deduplicate_observations`), so every audit artefact must be keyed by
    ``(organisation_number, id)``. Returns ``None`` when either part is missing or not a string.
    """
    organisation_number = item.get("organisation_number")
    observation_id = item.get("id")
    if isinstance(organisation_number, str) and organisation_number.strip() and isinstance(observation_id, str) and observation_id.strip():
        return (organisation_number, observation_id)
    return None


def build_audit_index(labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Index audit labels by composite identity and report every anomaly.

    Duplicate identical labels are folded and reported; conflicting labels for the same composite
    identity are reported and the identity is withheld from the usable index, because silently
    choosing one of two disagreeing human judgements would fabricate an audit result.
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    invalid: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for position, label in enumerate(labels, start=1):
        key = audit_identity(label)
        if key is None:
            invalid.append({
                "position": position,
                "id": label.get("id"),
                "organisation_number": label.get("organisation_number"),
                "reason": "label needs a string organisation_number and id",
            })
            continue
        if key in index:
            if index[key] == label:
                duplicates.append({"identity": list(key), "position": position})
            else:
                conflicts.append({"identity": list(key), "position": position})
            continue
        index[key] = label
    conflicted_keys = {tuple(entry["identity"]) for entry in conflicts}
    return {
        "identity_policy": AUDIT_IDENTITY_POLICY,
        "index": index,
        "usable": {key: value for key, value in index.items() if key not in conflicted_keys},
        "invalid": invalid,
        "duplicates": duplicates,
        "conflicts": conflicts,
    }


def audit_records(
    observations: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    *,
    minimum_audit: int = 100,
    organisation_numbers: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Derive the audit measurement from the observation and label artefacts.

    This is the single place where audit numbers are computed, so the evaluator report and the
    competition proxy cannot disagree about what was audited. Only published (policy-accepted)
    observations that carry a usable label for their composite identity count toward
    ``published_audited`` and ``audit_size_gate``; a label for an unpublished record, a label for an
    absent record, a duplicate label and a conflicting label are all reported separately.
    """
    audit = build_audit_index(labels)
    usable = audit["usable"]
    observation_keys = {key for key in (audit_identity(item) for item in observations) if key is not None}

    published: list[tuple[tuple[str, str], dict[str, Any]]] = []
    audited_unpublished: list[tuple[str, str]] = []
    for item in observations:
        key = audit_identity(item)
        if key is None or key not in usable:
            continue
        if publishable_observation(item):
            published.append((key, item))
        else:
            audited_unpublished.append(key)

    wrong_entity = sum(not usable[key].get("exact_entity", False) for key, _ in published)
    wrong_metric = sum(not usable[key].get("metric_correct", False) for key, _ in published)
    unsupported = sum(bool(validate_observation(item)) for _, item in published)
    sentiment_records = [(key, item) for key, item in published if item.get("sentiment_label") is not None]
    sentiment_correct = sum(bool(usable[key].get("sentiment_correct")) for key, _ in sentiment_records)

    def ratio(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    published_audited = len(published)
    entity_precision = ratio(published_audited - wrong_entity, published_audited)
    metric_precision = ratio(published_audited - wrong_metric, published_audited)
    sentiment_accuracy = ratio(sentiment_correct, len(sentiment_records)) if sentiment_records else None
    audit_size_gate = published_audited >= minimum_audit
    qualification = bool(
        audit_size_gate
        and published_audited
        and wrong_entity == 0
        and unsupported == 0
        and entity_precision >= 0.995
        and metric_precision >= 0.98
    )

    coverage_by_organisation: dict[str, dict[str, int]] = {}
    for org in sorted(set(organisation_numbers or []) | {key[0] for key in observation_keys} | {key[0] for key in usable}):
        coverage_by_organisation[org] = {
            "labels": sum(1 for key in usable if key[0] == org),
            "published_audited": sum(1 for key, _ in published if key[0] == org),
            "observations": sum(1 for key in observation_keys if key[0] == org),
        }

    return {
        "identity_policy": AUDIT_IDENTITY_POLICY,
        "labels_total": len(labels),
        "labels_valid": len(audit["index"]),
        "labels_usable": len(usable),
        "labels_invalid": audit["invalid"],
        "labels_duplicate": audit["duplicates"],
        "labels_conflicting": audit["conflicts"],
        "labels_without_observation": sorted(list(key) for key in set(usable) - observation_keys),
        "observations_without_labels": sorted(list(key) for key in observation_keys - set(usable)),
        "audited_observations": sum(1 for key in observation_keys if key in usable),
        "audited_unpublished": sorted(list(key) for key in audited_unpublished),
        "published_audited": published_audited,
        "audit_size": published_audited,
        "audit_size_gate": audit_size_gate,
        "minimum_audit": minimum_audit,
        "wrong_entity_publications": wrong_entity,
        "wrong_metric_publications": wrong_metric,
        "unsupported_publications": unsupported,
        "entity_precision": entity_precision,
        "metric_precision": metric_precision,
        "sentiment_audited": len(sentiment_records),
        "sentiment_accuracy": sentiment_accuracy,
        "qualification_passed": qualification,
        "coverage_by_organisation": coverage_by_organisation,
    }


def run_external_summary(
    gate: dict[str, Any],
    *,
    organisation_numbers: Iterable[str],
    enforce_freshness: bool = False,
) -> dict[str, Any]:
    """Run-level diagnostics and the two scorer inputs the real pipeline can produce.

    ``fresh_coverage`` and ``connector_policy_passed`` are defined here because the competition
    proxy consumes them (see ``scripts/score_competition_v3.py``); before this module existed no
    code produced them, so the proxy could never qualify.

    * ``fresh_coverage`` — share of batch organisations with at least one accepted observation
      inside the freshness window. The scorer multiplies this by the rubric's 3 freshness points.
    * ``connector_policy_passed`` — true when at least one observation is publishable, every
      published observation declares an approved acquisition mode and approved rights, and no
      published record used an experimental mode. This is an internal-consistency and policy gate
      over the data; it does **not** verify platform terms, which remain a human/legal declaration.
    """
    requested = [str(org) for org in organisation_numbers]
    by_organisation = gate["by_organisation"]
    accepted = gate["accepted"]
    fresh_orgs = [org for org in requested if by_organisation[org]["fresh_observations"] > 0]
    covered_orgs = [org for org in requested if by_organisation[org]["accepted_observations"] > 0]

    accepted_modes = sorted({str(item.get("acquisition_mode")) for item in accepted})
    accepted_rights = sorted({str(item.get("rights_status")) for item in accepted})
    experimental_published = sorted(set(accepted_modes) & EXPERIMENTAL_ACQUISITION_MODES)
    unapproved_published = sorted(set(accepted_modes) - PUBLISHABLE_ACQUISITION_MODES)
    connector_policy_passed = bool(
        accepted
        and accepted_rights == ["approved"]
        and not unapproved_published
        and not experimental_published
    )

    rejection_reasons: dict[str, int] = {}
    for entry in gate["rejected"]:
        for reason in entry["reasons"]:
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    return {
        "policy_version": gate["policy_version"],
        "schema_version": gate["schema_version"],
        "counts_scope": gate["counts_scope"],
        "freshness_days": gate["freshness_days"],
        "as_of": gate["as_of"],
        "hash_verification": gate["hash_verification"],
        "records_read": gate["duplicates"]["input_records"],
        "unique_records": gate["duplicates"]["unique_records"],
        "accepted": len(accepted),
        "rejected": len(gate["rejected"]),
        "unmatched": len(gate["unmatched"]),
        "malformed": len(gate["malformed"]),
        "duplicates_collapsed": gate["duplicates"]["collapsed_identical"],
        "conflicting_duplicate_groups": len(gate["duplicates"]["conflicting_groups"]),
        "conflicting_duplicate_records": gate["duplicates"]["conflicted_records"],
        "ids_shared_across_organisations": gate["duplicates"]["ids_shared_across_organisations"],
        "content_duplicate_ids": gate["duplicates"]["content_duplicate_ids"],
        "stale_observations": sum(by_organisation[org]["stale_observations"] for org in requested),
        "fresh_observations": sum(by_organisation[org]["fresh_observations"] for org in requested),
        "organisations_with_publishable_observation": len(covered_orgs),
        "organisations_with_fresh_observation": len(fresh_orgs),
        "fresh_coverage": round(len(fresh_orgs) / len(requested), 6) if requested else 0.0,
        "publishable_coverage": round(len(covered_orgs) / len(requested), 6) if requested else 0.0,
        "connector_policy_passed": connector_policy_passed,
        "connector_policy": {
            "definition": "published observations must use approved acquisition modes and approved rights; experimental output never publishes",
            "acquisition_modes_published": accepted_modes,
            "rights_statuses_published": accepted_rights,
            "experimental_modes_published": experimental_published,
            "unapproved_modes_published": unapproved_published,
            "platform_terms_verified_here": False,
            "note": "Platform terms and licence acceptance remain a human declaration; this gate only proves the record-level policy holds.",
        },
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "malformed_records": list(gate["malformed"]),
        "unmatched_records": list(gate["unmatched"]),
        "duplicate_conflicts": list(gate["duplicates"]["conflicting_groups"]),
        "counts_enforced_freshness": enforce_freshness,
    }


def company_external_block(gate: dict[str, Any], organisation_number: str) -> dict[str, Any] | None:
    """Build the per-organisation external block attached to profiles and envelopes.

    Returns ``None`` when the organisation has no accepted observation, so the caller can keep
    the key absent (matching the existing convention that a source that was not checked is not
    the same as a source that returned nothing).
    """
    org = str(organisation_number)
    summary = gate["by_organisation"].get(org)
    if not summary or summary["accepted_observations"] == 0:
        return None
    records = [item for item in gate["accepted"] if str(item.get("organisation_number")) == org]
    # The footprint covers the organisation's gated record set: the accepted records plus this
    # organisation's rejected records. Acceptance/freshness counters are unaffected (they are
    # derived from the publishable subset), but the published rejection count now describes the
    # rejection diagnostics published beside it instead of being a constant zero.
    gated_records = records + [
        entry for entry in gate["rejected"] if entry["organisation_number"] == org
    ]
    footprint = aggregate_footprint(
        gated_records,
        as_of=gate["as_of"],
        freshness_days=gate["freshness_days"],
        enforce_freshness=gate["counts_scope"] == "fresh",
    )
    published_ids = sorted(str(item.get("id")) for item in records)
    stale_ids = sorted(
        str(item.get("id"))
        for item in records
        if not _is_fresh(item, as_of=gate["as_of"], freshness_days=gate["freshness_days"])
    )
    block: dict[str, Any] = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "policy_version": gate["policy_version"],
        "organisation_number": org,
        "status": footprint["status"],
        "counts_scope": footprint["counts_scope"],
        "publishable_only": True,
        "accepted_observation_ids": published_ids,
        "stale_observation_ids": stale_ids,
        "rejected_observations": summary["rejected"],
        "footprint": footprint,
        "hash_verification": HASH_VERIFICATION,
    }
    return block


def _is_fresh(item: dict[str, Any], *, as_of: str | None, freshness_days: int) -> bool:
    now = parse_timestamp(as_of)
    retrieved = parse_timestamp(item.get("retrieved_at"))
    if now is None or retrieved is None:
        return False
    delta = (now - retrieved).total_seconds()
    return 0 <= delta <= freshness_days * 86_400
