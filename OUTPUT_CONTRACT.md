# Minimal output contract

Emit one JSON object per input organisation number.

```json
{
  "organisation_number": "123456789",
  "run": {
    "run_id": "2026-08-24-a",
    "started_at": "2026-08-24T06:00:00Z",
    "completed_at": "2026-08-24T06:00:08Z",
    "terminal_status": "completed"
  },
  "claims": [
    {
      "field": "official_website",
      "value": "https://example.no/",
      "availability": "available",
      "confidence": 0.99,
      "evidence_ids": ["ev-1"]
    }
  ],
  "evidence": [
    {
      "id": "ev-1",
      "source_url": "https://example.no/",
      "source_class": "company_owned",
      "retrieved_at": "2026-08-24T06:00:04Z",
      "content_sha256": "...",
      "claim_span": "Example AS, organisation number 123 456 789"
    }
  ],
  "changes": [],
  "errors": [],
  "operations": {
    "requests": 4,
    "runtime_ms": 8120,
    "third_party_cost_usd": 0
  }
}
```

Allowed availability states are `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous` and `failed`. A checked source that has zero jobs or zero locations is different from a source that was not checked.

## Implementation status (added 2026-10-02)

This section records how `src/norway_company_agent/batch.py` implements the contract above. It does
not change the contract; where the document was ambiguous the choice made in code is stated
explicitly so it can be reviewed or overridden.

Implemented in `contract_envelope()` / `validate_contract_envelope()`, emitted by
`scripts/run_competition_batch.py --output`. `contract_version` is `signalpost-minimal-v1`.

| Documented item | Implementation |
| --- | --- |
| `run.{run_id, started_at, completed_at, terminal_status}` | Emitted. The document does not enumerate `terminal_status` values; the implementation emits `completed` or `failed` only (entity level). The per-module states remain in the legacy `modules` map. |
| `claims[]` | One claim per requested module, `field` = module name, `value` = the module's normalised value when available and `null` otherwise, `evidence_ids` = the single evidence id that supports it. |
| `confidence` | The document shows a number but does not define how to compute it. The implementation emits a deterministic presence indicator: `1.0` when the module is `available`, `0.0` otherwise. It is **not** a calibrated probability and no uncertainty model backs it. |
| `evidence[]` | The documented fields only, enforced by an allow-list in the validator (2026-10-03: a record carrying an undocumented key is refused, so internal fields cannot be renamed past a deny-list). `content_sha256` is `null` when a source failed before bytes were captured; it is a 64-character lowercase hex digest otherwise. `claim_span` is the source's own note when present, otherwise a deterministic summary of the returned value (never an invented quotation). |
| `changes[]` | Always `[]` in a batch run: a batch run has no previous snapshot to diff. Change events are produced by `scripts/run_refresh_replay.py`. |
| `errors[]` | The document shows an empty array and gives no entry schema. The implementation emits `{module, availability, state, note, source_url}` for every module whose availability is `blocked` or `failed`. |
| `operations` | `requests` and `runtime_ms` come from the profile's own run metrics; `third_party_cost_usd` is `0` for this pipeline because it calls only the official Brønnøysund registry API and the company's own website. |
| availability vocabulary | Evidence states map as: `available`→`available`, `not_found`→`not_available`, `not_applicable`→`not_applicable`, `blocked`→`blocked`, `source_error`→`failed`, `not_fetched`→`not_available` (lossy: the precise state stays in `modules`). The documented state `ambiguous` is accepted by the validator but no producer emits it yet. |
| unmapped extension | Each envelope may carry an `external` block (external observations for that organisation). It is an extension, not part of this contract; consumers that validate the documented fields can ignore it. External observations never enter `claims`, `evidence`, `errors` or `profile.evidence`. |

Legacy keys (`run_id`, `state`, `started_at`, `completed_at`, `modules`, `profile`) are retained so
existing consumers keep working.

