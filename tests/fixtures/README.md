# Fixture provenance

Every file in this directory is a **synthetic fixture**: invented, deterministic, and safe to
publish. Nothing here is real company data, a real connector response, a real audit label, or a
real measurement. Fixture values must never be cited as evidence about any organisation.

| File | Kind | Purpose |
| --- | --- | --- |
| `sentiment-*.jsonl`, `research-*.md`, `refresh-snapshots.json`, `*-snapshot*` | synthetic (pre-existing) | Unit coverage for normalizers, sentiment parsing, refresh diffing. |
| `bulk-registry-sample.csv.gz` | synthetic | Three fictional registry rows (one ASA, one AS, one foundation) in the Brreg bulk schema (`;`-separated, gzipped, deterministic mtime=0). Lets the competition CLI and the e2e runner execute without the `.gitignore`d production snapshot. |
| `batch-orgs-3.jsonl` | synthetic | Organisation input list matching the bulk sample, with `evaluation_split`/`sample_slice` annotations. |
| `external-observations-valid.jsonl` | synthetic | Three publishable observations: one Google-Places-style summary, one company-site post, one licensed-news mention carrying a sentiment label. |
| `external-observations-adversarial.jsonl` | synthetic | 20 records designed to fail policy: out-of-batch org, unapproved rights, experimental acquisition mode, non-hex hash, missing hash, unsupported platform, missing evidence span, `exact_entity=false`, missing identity proof, stale 2020 job posting, identical duplicate, conflicting duplicate, duplicate id reused across two organisations, malformed JSON, non-object JSON, missing id, 8-digit organisation number. |
| `external-audit-labels.synthetic.jsonl` | synthetic | Labels for three records, used only to prove the evaluator's label mechanics run. The filename and the `note` field say *synthetic*: this is **not** an independent human audit and it cannot satisfy the evaluator's 100-record audit gate (it is deliberately below it). |

`content_sha256` values in the observation fixtures are `sha256("synthetic-fixture:<label>")`,
i.e. reproducible digests of a name, not of any fetched bytes. They exist so the fixtures carry
syntactically valid 64-character lowercase hex digests. A non-hex value (`zz…`) and a missing value
are included deliberately so the hash-syntax path is exercised.
