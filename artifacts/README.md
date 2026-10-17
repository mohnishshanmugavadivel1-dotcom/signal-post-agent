# Tracked run artefacts

This directory is the tracked home for the **real** 100-company smoke-test result that the
submission contract asks for (`README.md`, "Submission contract"). Everything under `out/` is
gitignored scratch output; a result is published only by copying it here deliberately, after it has
been verified.

Status (2026-10-17): **no smoke-test report has been produced yet.** The run requires live access to
`data.brreg.no` (registry snapshot download and the open registry APIs) and to the companies'
registry-listed websites. The sandbox in which the recent engineering work was done could not reach
those hosts (`curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to data.brreg.no:443`),
so nothing here is claimed. Offline runs over synthetic fixtures are mechanics tests and must never be
copied into this directory.

## Layout

```
artifacts/
  README.md                     this file
  smoke-001/
    report.json                 copy of out/smoke-report.json  (machine-readable run report, ~4 kB)
    RUN.md                      operator record: date, machine, Python/uv versions, exact command,
                                verifier output, sha256 of report/envelopes/profiles/bulk snapshot
```

Only the report and the operator record are tracked by default. Envelopes (`out/smoke-envelopes.jsonl`,
several MB for 100 companies with live payloads) and profiles embed public registry data (Brønnøysund open data,
NLOD 2.0 — attribution required), names and roles of public role holders (personal data, even though
public; birth dates are never stored) and short excerpts of company websites (copyright review). Keep
them untracked, or track them only after a deliberate privacy and source-rights review, and never
track `brreg-enheter.csv` (large raw snapshot; already ignored).

## Producing the result on a network-enabled machine

```bash
uv sync
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
gzip -t brreg-enheter.csv   # the loader expects gzip bytes; recompress with gzip if this fails

uv run python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 100
```

No API key or account is needed for the default modules. The runner exits `0` only when all 100
envelopes validated; a non-zero exit means the report's `validation` / `contract_status` blocks name
the problem. Module-level failures (a company with no website, a 404 for group structure, a robots
refusal) are legitimate outcomes recorded inside the envelopes — they do not make the run fail and
must not be edited out.

## Verifying the result before publishing it

```bash
uv run python scripts/verify_smoke_artifacts.py \
  --report out/smoke-report.json \
  --envelopes out/smoke-envelopes.jsonl \
  --organisations smoke-companies.jsonl \
  --expected-count 100 --run-id smoke-001 \
  --json-output out/smoke-verify.json
```

The verifier exits `0` only when: the report and the envelope file both contain exactly 100
envelopes for exactly the committed organisation numbers, in input order, with no duplicates;
`validation.passed` is true and `contract_status` reports 100/100; every envelope re-validates against
the contract and the organiser's public citation checks; `run_id` and `contract_version` agree
across report and envelopes; the report's request total equals the sum of the per-envelope request
counts; the cost aggregate equals the sum of the per-envelope declarations and is complete; the
registry snapshot digest is recorded; and no credential-like strings appear in either file. It also
prints the SHA-256 digests, runtime, request count, terminal-status and per-module availability
breakdown to copy into `RUN.md`.

## Publishing

```bash
mkdir -p artifacts/smoke-001
cp out/smoke-report.json artifacts/smoke-001/report.json
# write artifacts/smoke-001/RUN.md from the verifier output, then:
git add artifacts/smoke-001 && git commit -m "Add smoke-001 run report (100 companies, live registry)"
```

Record in `RUN.md`, verbatim: the command, the date/time, `python --version` and `uv --version`,
the verifier's PASSED line and the "Record with the artefact" block (digests of the report,
envelopes, profiles and `brreg-enheter.csv`), and the `third_party_cost_usd` basis (declared
`0.0` per envelope for the default, unmetered modules — see `README.md`, "Cost basis").
