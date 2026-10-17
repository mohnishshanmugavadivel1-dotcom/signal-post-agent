from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


PLATFORMS = {
    "company_site",
    "google_places",
    "linkedin",
    "facebook",
    "instagram",
    "x",
    "youtube",
    "tiktok",
    "glassdoor",
    "indeed",
    "job_board",
    "news",
    "openstreetmap",
    "brreg",
    "apple_app_store",
    "google_play",
    "wikidata",
    "wikipedia",
    "company_directory",
}

SIGNAL_TYPES = {
    "company_profile",
    "profile_handle",
    "profile_metrics",
    "place_summary",
    "review",
    "review_summary",
    "job_posting",
    "workforce_snapshot",
    "public_post",
    "public_mention",
    "buzz_metrics",
}

# “Experimental” means the connector can be benchmarked locally, but its output cannot be
# published or earn competition points until the organiser accepts its rights/reliability path.
PUBLISHABLE_ACQUISITION_MODES = {
    "official_api",
    "licensed_api",
    "company_authorized_export",
    "permitted_public_page",
}
EXPERIMENTAL_ACQUISITION_MODES = {
    "jobspy_experiment",
    "unofficial_api_experiment",
    "rights_review_experiment",
}
INDEPENDENT_SENTIMENT_CLASSES = {"customer_review", "employee_review", "licensed_news", "public_news", "public_mention"}


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: Engagement counters the aggregator actually sums. Other `metrics` keys stay source-specific.
AGGREGATED_METRIC_FIELDS = ("likes", "comments", "shares")


def _is_organisation_number(value: Any) -> bool:
    """A valid Norwegian organisation number is exactly nine ASCII digits."""
    return isinstance(value, str) and len(value) == 9 and value.isascii() and value.isdigit()


#: Public name for the same policy, so the contract validator (`batch.py`) and the observation gate
#: cannot drift apart on what counts as a valid organisation number.
is_organisation_number = _is_organisation_number


def diagnostic_id(value: Any) -> str | None:
    """JSON-safe identifier for a diagnostic record.

    A rejected record can carry any JSON value as its ``id``. Diagnostics are consumed by tooling
    that may key them by id, so a non-string id is rendered as canonical JSON text instead of being
    republished with its original type (which would also make it unusable as a mapping key).
    """
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").casefold().removeprefix("www.")


def observation_fingerprint(item: dict[str, Any]) -> str:
    """Deterministic content fingerprint of an observation payload.

    Used to collapse identical duplicates and to detect conflicting reuse of the
    same (organisation_number, id) pair. The payload is canonicalised with sorted
    keys and no insignificant whitespace, so field order cannot change the result.
    """
    return hashlib.sha256(
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def validate_observation(item: dict[str, Any], *, organisation_number: str | None = None) -> list[str]:
    """Validate one observation against the publication policy.

    ``organisation_number`` binds the observation to the organisation whose profile
    is being built. Without that argument the check is structural only, and an
    observation for any organisation validates — callers that assemble company
    profiles must pass the expected organisation number.
    """
    reasons: list[str] = []

    # Identifier fields are typed before they are used as mapping keys or set members. Membership
    # tests on unhashable values raise TypeError, which would abort a whole batch, so the type
    # check comes first and the value is never coerced.
    observation_id = item.get("id")
    if observation_id is None or (isinstance(observation_id, str) and not observation_id.strip()):
        reasons.append("missing observation id")
    elif not isinstance(observation_id, str):
        reasons.append("observation id must be a string")

    if not _is_organisation_number(item.get("organisation_number")):
        reasons.append("missing or invalid organisation number")

    platform = item.get("platform")
    if not isinstance(platform, str) or platform not in PLATFORMS:
        reasons.append("unsupported platform")

    signal_type = item.get("signal_type")
    signal_type_ok = isinstance(signal_type, str) and signal_type in SIGNAL_TYPES
    if not signal_type_ok:
        reasons.append("unsupported signal type")

    source_url = item.get("source_url")
    if not isinstance(source_url, str) or not source_url.strip() or not _host(source_url):
        reasons.append("missing or invalid source URL")

    retrieved_at = item.get("retrieved_at")
    if retrieved_at is None or (isinstance(retrieved_at, str) and not retrieved_at.strip()):
        reasons.append("missing retrieval time")
    elif not isinstance(retrieved_at, str):
        reasons.append("retrieval time must be a string")

    digest = item.get("content_sha256")
    if digest is None or (isinstance(digest, str) and not digest):
        reasons.append("missing content hash")
    elif not isinstance(digest, str) or not SHA256_PATTERN.match(digest):
        reasons.append("invalid content hash")

    if item.get("exact_entity") is not True:
        reasons.append("exact legal entity is not verified")

    proof = item.get("identity_proof")
    if not isinstance(proof, list) or not proof:
        reasons.append("missing exact-entity proof")
    elif not all(isinstance(entry, dict) for entry in proof):
        reasons.append("identity proof entries must be objects")

    acquisition_mode = item.get("acquisition_mode")
    if not isinstance(acquisition_mode, str) or acquisition_mode not in PUBLISHABLE_ACQUISITION_MODES:
        reasons.append("acquisition mode is not approved for publication")

    if item.get("rights_status") != "approved":
        reasons.append("source rights are not approved")

    if signal_type_ok and signal_type in {"review", "public_post", "public_mention"}:
        span = item.get("evidence_span")
        if not isinstance(span, str) or not span.strip():
            reasons.append("missing evidence span")

    metrics = item.get("metrics")
    if metrics is not None and not isinstance(metrics, dict):
        reasons.append("metrics must be an object")
    elif isinstance(metrics, dict):
        for field in AGGREGATED_METRIC_FIELDS:
            value = metrics.get(field)
            if value is not None and (not _is_number(value) or float(value) < 0):
                reasons.append(f"metric {field} must be a non-negative number")

    reviewer_id = item.get("reviewer_id")
    if reviewer_id is not None and not isinstance(reviewer_id, str):
        reasons.append("reviewer id must be a string")

    sentiment_label = item.get("sentiment_label")
    if sentiment_label is not None:
        if not isinstance(sentiment_label, str) or sentiment_label not in {"positive", "neutral", "negative", "mixed"}:
            reasons.append("unsupported sentiment label")
        source_class = item.get("source_class")
        if not isinstance(source_class, str) or source_class not in INDEPENDENT_SENTIMENT_CLASSES:
            reasons.append("sentiment source is not independent")
        model_version = item.get("sentiment_model_version")
        if not isinstance(model_version, str) or not model_version.strip():
            reasons.append("missing sentiment model version")

    if organisation_number is not None:
        expected = str(organisation_number)
        observed = item.get("organisation_number")
        if not isinstance(observed, str) or observed != expected:
            reasons.append("observation organisation number does not match the profile")
    return reasons


def publishable_observation(item: dict[str, Any], *, organisation_number: str | None = None) -> bool:
    return not validate_observation(item, organisation_number=organisation_number)


def _as_datetime(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_timestamp(value: str | None) -> datetime | None:
    """Public ISO-8601 parser used by the freshness policy (None when unparseable)."""
    return _as_datetime(value)


def aggregate_footprint(
    observations: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    freshness_days: int = 45,
    enforce_freshness: bool = False,
) -> dict[str, Any]:
    """Create a conservative company-level external-footprint summary.

    Counts are kept source-specific. They are not added into a fake universal “popularity” number.
    The normalized buzz and sentiment scores are only emitted when their minimum evidence gates pass.

    Acceptance and freshness are deliberately separate:

    * ``accepted_*`` counts describe the publication-policy decision for the input records.
    * ``fresh_*`` counts describe records retrieved inside the freshness window.
    * ``counted_*`` describes which set produced the platform/signal counters.

    With ``enforce_freshness=False`` (the default, and the historical behaviour) counters include
    every accepted record and stale records are only reported as a count. With
    ``enforce_freshness=True`` the counters, the sentiment gate and the sentiment score use fresh
    records only. Callers that publish the summary must state which scope they used; the
    ``counts_scope`` field records it.

    Records with a missing or unparseable ``retrieved_at`` are accepted are never fresh: they cannot
    be placed inside the freshness window, so they are excluded from ``fresh_*`` counters.
    """

    accepted = [item for item in observations if publishable_observation(item)]
    rejected = [
        {"id": diagnostic_id(item.get("id")), "reasons": validate_observation(item)}
        for item in observations
        if not publishable_observation(item)
    ]
    now = _as_datetime(as_of) or datetime.now(timezone.utc)
    cutoff_seconds = freshness_days * 86_400
    fresh = []
    undated = 0
    for item in accepted:
        retrieved = _as_datetime(item.get("retrieved_at"))
        if retrieved is None:
            undated += 1
        elif 0 <= (now - retrieved).total_seconds() <= cutoff_seconds:
            fresh.append(item)
    counted = fresh if enforce_freshness else accepted

    by_platform: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in counted:
        by_platform[item["platform"]].append(item)

    review_items = [item for item in counted if item["signal_type"] in {"review", "review_summary"}]
    job_items = [item for item in counted if item["signal_type"] == "job_posting"]
    public_items = [item for item in counted if item["signal_type"] in {"public_post", "public_mention", "buzz_metrics"}]
    sentiment_items = [item for item in counted if item.get("sentiment_label")]
    independent_sentiment_hosts = {_host(str(item["source_url"])) for item in sentiment_items}
    # A reviewer only provides an independent opinion once per source; the same reviewer on the
    # same host is one opinion, not two.
    independent_sentiment_pairs = {
        (_host(str(item["source_url"])), str(item.get("reviewer_id")))
        for item in sentiment_items
        if item.get("reviewer_id")
    }
    independent_sentiment_reviewers = {reviewer for _, reviewer in independent_sentiment_pairs}

    label_values = {"negative": -1, "neutral": 0, "positive": 1}
    scalar_sentiment = [label_values[item["sentiment_label"]] for item in sentiment_items if item["sentiment_label"] in label_values]
    sentiment_ready = len(sentiment_items) >= 10 and (
        len(independent_sentiment_hosts) >= 2 or len(independent_sentiment_reviewers) >= 10
    )
    sentiment_score = round(50 + 50 * sum(scalar_sentiment) / len(scalar_sentiment), 1) if sentiment_ready and scalar_sentiment else None

    def engagement_value(item: dict[str, Any], field: str) -> int:
        """Read one engagement counter without trusting the input shape.

        The gate rejects malformed metrics before aggregation, but this function is also reachable
        directly (tests, evaluator scripts), so it stays defensive: a metrics value that is not a
        finite, non-negative number contributes nothing instead of raising.
        """
        metrics = item.get("metrics")
        if not isinstance(metrics, dict):
            return 0
        value = metrics.get(field)
        if not _is_number(value) or float(value) < 0:
            return 0
        return int(value)

    engagement = sum(engagement_value(item, field) for item in public_items for field in AGGREGATED_METRIC_FIELDS)
    unique_public_items = len({str(item.get("source_url")) for item in public_items})

    return {
        "status": "available" if accepted else "not_available",
        "accepted_observations": len(accepted),
        "counted_observations": len(counted),
        "counts_scope": "fresh" if enforce_freshness else "accepted",
        "rejected_observations": len(rejected),
        "rejections": rejected,
        "platforms": sorted(by_platform),
        "platform_counts": {platform: len(items) for platform, items in sorted(by_platform.items())},
        "fresh_observations": len(fresh),
        "stale_observations": len(accepted) - len(fresh),
        "undated_observations": undated,
        "freshness_days": freshness_days,
        "fresh_platform_counts": dict(sorted(Counter(item["platform"] for item in fresh).items())),
        "fresh_review_signal_count": len([item for item in fresh if item["signal_type"] in {"review", "review_summary"}]),
        "fresh_active_job_count": len({str(item.get("source_url")) for item in fresh if item["signal_type"] == "job_posting"}),
        "fresh_public_item_count": len({str(item.get("source_url")) for item in fresh if item["signal_type"] in {"public_post", "public_mention", "buzz_metrics"}}),
        "freshness_policy": {
            "as_of": (now.isoformat().replace("+00:00", "Z")),
            "freshness_days": freshness_days,
            "enforced": enforce_freshness,
            "undated_is_fresh": False,
            "stale_is_publishable": True,
        },
        "review_signal_count": len(review_items),
        "active_job_count": len({str(item.get("source_url")) for item in job_items}),
        "public_item_count": unique_public_items,
        "public_engagement": engagement,
        "sentiment": {
            "status": "available" if sentiment_ready else "abstain",
            "score_0_100": sentiment_score,
            "items": len(sentiment_items),
            "independent_sources": len(independent_sentiment_hosts),
            "independent_reviewers": len(independent_sentiment_reviewers),
            "label_counts": dict(Counter(item["sentiment_label"] for item in sentiment_items)),
            "warning": "Dated contextual signal, not a timeless fact about the company.",
        },
    }
