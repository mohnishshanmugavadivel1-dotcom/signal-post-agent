"""D1-D5 regression tests for the Signalpost repair sprint.

Exercises the real production code paths.  Each test is documented with:
  - the defect it covers
  - the failing assertion on the unfixed code
  - what the fix must ensure

Run against the UNFIXED copy to capture baseline failures, then again after
the minimal fixes to confirm the repairs.
"""
from __future__ import annotations

from typing import Any
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(org: str = "123456789", **overrides) -> dict:
    profile: dict[str, Any] = {
        "organisation_number": org,
        "name": "Testvakt AS",
        "legal_form": "AS",
        "employees": 5,
        "municipality": "OSLO",
        "website": None,
        "latest_submitted_accounts": "2022",
        "evidence": {
            "registry": {
                "status": "available",
                "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/123456789",
                "source_class": "official_registry_live",
                "retrieved_at": "2024-01-01T00:00:00+00:00",
                "content_sha256": "a" * 64,
                "value": {
                    "organisation_number": org,
                    "name": "Testvakt AS",
                    "legal_form": "AS",
                    "employees": 5,
                    "bankrupt": False,
                    "liquidating": False,
                },
            },
            "financials": {
                "status": "available",
                "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/123456789",
                "source_class": "official_annual_accounts",
                "retrieved_at": "2024-01-01T00:00:00+00:00",
                "content_sha256": "b" * 64,
                "value": {"records": []},
            },
        },
    }
    profile.update(overrides)
    return profile


# ===========================================================================
# D1 — Company status: deregistered companies classified as active
# ===========================================================================

class TestD1CompanyStatus:
    """
    Defect: stratum() classifies a company as active whenever bankrupt=False and
    liquidating=False, even when slettedato or underTvangsavvikling indicate
    the company is deleted or force-dissolved.  normalize_row() does not capture
    these fields so they cannot enter the stratum logic.

    PRE-FIX: both tests below FAIL (state_part == "active").
    POST-FIX: both tests PASS (state_part != "active").
    """

    def test_deleted_company_classified_adverse(self):
        """A row with slettedato set must NOT receive state=active in its stratum."""
        from norway_company_agent.sampling import normalize_row, stratum

        row = {
            "organisasjonsnummer": "111111111",
            "navn": "Slettet AS",
            "organisasjonsform.kode": "AS",
            "antallAnsatte": "0",
            "konkurs": "false",
            "underAvvikling": "false",
            "slettedato": "2023-06-01",
            "forretningsadresse.kommune": "OSLO",
            "forretningsadresse.kommunenummer": "0301",
            "naeringskode1.kode": "62.010",
            "naeringskode1.beskrivelse": "Utvikling",
            "hjemmeside": "",
            "sisteInnsendteAarsregnskap": "",
        }
        record = normalize_row(row)
        s = stratum(record)
        state_part = s.split("|")[2]
        assert state_part != "active", (
            f"D1: deleted company (slettedato set) classified as state=active; "
            f"full stratum={s!r}"
        )

    def test_force_dissolved_company_classified_adverse(self):
        """A row with underTvangsavvikling=true must NOT be classified as active."""
        from norway_company_agent.sampling import normalize_row, stratum

        row = {
            "organisasjonsnummer": "222222222",
            "navn": "Tvangsavviklet AS",
            "organisasjonsform.kode": "AS",
            "antallAnsatte": "0",
            "konkurs": "false",
            "underAvvikling": "false",
            "slettedato": "",
            "underTvangsavvikling": "true",
            "forretningsadresse.kommune": "",
            "forretningsadresse.kommunenummer": "",
            "naeringskode1.kode": "",
            "naeringskode1.beskrivelse": "",
            "hjemmeside": "",
            "sisteInnsendteAarsregnskap": "",
        }
        record = normalize_row(row)
        s = stratum(record)
        state_part = s.split("|")[2]
        assert state_part != "active", (
            f"D1: force-dissolved company (underTvangsavvikling=true) classified as "
            f"state=active; full stratum={s!r}"
        )

    def test_valid_active_company_still_active(self):
        """Edge-case guard: a genuinely active company must remain active."""
        from norway_company_agent.sampling import normalize_row, stratum

        row = {
            "organisasjonsnummer": "333333333",
            "navn": "Aktiv AS",
            "organisasjonsform.kode": "AS",
            "antallAnsatte": "10",
            "konkurs": "false",
            "underAvvikling": "false",
            "slettedato": "",
            "underTvangsavvikling": "false",
            "forretningsadresse.kommune": "BERGEN",
            "forretningsadresse.kommunenummer": "4601",
            "naeringskode1.kode": "47.110",
            "naeringskode1.beskrivelse": "Dagligvarehandel",
            "hjemmeside": "",
            "sisteInnsendteAarsregnskap": "2023",
        }
        record = normalize_row(row)
        s = stratum(record)
        state_part = s.split("|")[2]
        assert state_part == "active", (
            f"D1 guard: genuinely active company classified as {state_part!r}; "
            f"full stratum={s!r}"
        )


# ===========================================================================
# D2 — Organisation identity: mismatched org number in live registry payload
# ===========================================================================

class TestD2OrganisationIdentity:
    """
    Defect: fetch_official_modules() calls normalize_entity() and stores the result as
    status=available without checking that the response body organisasjonsnummer matches
    the requested org.

    PRE-FIX: test_mismatched_org_number_rejected FAILS (status == available).
    POST-FIX: mismatch status is source_error; matching stays available.
    """

    def _fake_fetcher(self, response_org):
        from norway_company_agent.http import FetchResult
        def fetcher(url: str) -> FetchResult:
            body = ({"organisasjonsnummer": response_org, "navn": "Some Company AS"}
                    if response_org is not None else {"navn": "No Org AS"})
            return FetchResult(url=url, status=200, elapsed_ms=10, bytes_received=100, body=body, error=None,
                               content_sha256="c" * 64,
                               retrieved_at="2024-01-01T00:00:00+00:00", effective_at=None)
        return fetcher

    def test_mismatched_org_number_rejected(self):
        """Live registry response for a different org must NOT be status=available."""
        from norway_company_agent.official import fetch_official_modules

        records, _ = fetch_official_modules(
            "123456789", {"registry_live"}, fetcher=self._fake_fetcher("999888777"))
        reg = records.get("registry_live", {})
        assert reg.get("status") != "available", (
            f"D2: mismatched org payload accepted as status={reg.get('status')!r}; "
            "expected source_error"
        )
        note = str(reg.get("note") or "")
        assert "999888777" in note or "mismatch" in note.lower(), (
            f"D2: mismatch note absent; note={note!r}"
        )

    def test_matching_org_number_accepted(self):
        """A response with the correct org number must remain available."""
        from norway_company_agent.official import fetch_official_modules

        records, _ = fetch_official_modules(
            "123456789", {"registry_live"}, fetcher=self._fake_fetcher("123456789"))
        reg = records.get("registry_live", {})
        assert reg.get("status") == "available", (
            f"D2 guard: matching org rejected as {reg.get('status')!r}"
        )

    def test_missing_org_number_in_response_rejected(self):
        """A response body without an org number must NOT be merged as valid identity."""
        from norway_company_agent.official import fetch_official_modules

        records, _ = fetch_official_modules(
            "123456789", {"registry_live"}, fetcher=self._fake_fetcher(None))
        reg = records.get("registry_live", {})
        assert reg.get("status") != "available", (
            f"D2: response missing org number accepted as status={reg.get('status')!r}"
        )

    def test_malformed_org_number_rejected(self):
        """A response with a non-9-digit org number must be rejected."""
        from norway_company_agent.official import fetch_official_modules

        records, _ = fetch_official_modules(
            "123456789", {"registry_live"}, fetcher=self._fake_fetcher("abc"))
        reg = records.get("registry_live", {})
        assert reg.get("status") != "available", (
            f"D2: malformed org number accepted as status={reg.get('status')!r}"
        )

    def test_normalised_org_number_with_spaces_accepted(self):
        """Org number with surrounding whitespace must be normalised before comparison."""
        from norway_company_agent.official import fetch_official_modules
        from norway_company_agent.http import FetchResult

        def fetcher(url: str) -> FetchResult:
            body = {"organisasjonsnummer": " 123456789 ", "navn": "Spaced AS"}
            return FetchResult(url=url, status=200, elapsed_ms=10, bytes_received=100, body=body, error=None,
                               content_sha256="e" * 64,
                               retrieved_at="2024-01-01T00:00:00+00:00", effective_at=None)

        records, _ = fetch_official_modules("123456789", {"registry_live"}, fetcher=fetcher)
        reg = records.get("registry_live", {})
        assert reg.get("status") == "available", (
            f"D2 guard: org with whitespace rejected as {reg.get('status')!r}"
        )


# ===========================================================================
# D3 — Employee counts: negative values enter strata and research answers
# ===========================================================================

class TestD3EmployeeCounts:
    """
    Defect: normalize_row() accepts negative integers for employees.  stratum() uses
    the negative value in comparison bands, producing a wrong stratum.  answer_profile()
    emits the raw employees value including negatives.

    PRE-FIX: test_normalize_row_rejects_negative_employees FAILS (employees == -5).
    POST-FIX: employees is None; stratum band is 'missing'; no negative claim emitted.
    """

    def test_normalize_row_rejects_negative_employees(self):
        """normalize_row must map a negative employee count to None."""
        from norway_company_agent.sampling import normalize_row

        row = {
            "organisasjonsnummer": "444444444",
            "navn": "Negativ AS",
            "organisasjonsform.kode": "AS",
            "antallAnsatte": "-5",
            "konkurs": "false",
            "underAvvikling": "false",
            "forretningsadresse.kommune": "OSLO",
            "forretningsadresse.kommunenummer": "0301",
            "naeringskode1.kode": "",
            "naeringskode1.beskrivelse": "",
            "hjemmeside": "",
            "sisteInnsendteAarsregnskap": "",
        }
        record = normalize_row(row)
        assert record["employees"] is None, (
            f"D3: negative employee count not rejected; got employees={record['employees']}"
        )

    def test_stratum_negative_employees_uses_missing_band(self):
        """After fix, a record with employees=None must use the missing band."""
        from norway_company_agent.sampling import normalize_row, stratum

        row = {
            "organisasjonsnummer": "444444444",
            "navn": "Negativ AS",
            "organisasjonsform.kode": "AS",
            "antallAnsatte": "-5",
            "konkurs": "false",
            "underAvvikling": "false",
            "forretningsadresse.kommune": "OSLO",
            "forretningsadresse.kommunenummer": "0301",
            "naeringskode1.kode": "",
            "naeringskode1.beskrivelse": "",
            "hjemmeside": "",
            "sisteInnsendteAarsregnskap": "",
        }
        record = normalize_row(row)
        s = stratum(record)
        band = s.split("|")[1]
        assert band == "missing", (
            f"D3: negative employees produced band={band!r}; expected missing. stratum={s!r}"
        )

    def test_answer_profile_no_negative_employee_claim(self):
        """answer_profile must not emit a claim with a negative employee count."""
        from norway_company_agent.research import answer_profile

        profile = _make_profile(employees=-3)
        result = answer_profile(profile, "How many employees does this company have?")
        for fact in result["facts"]:
            if "employee" in fact["claim"].lower():
                val = fact["value"]
                assert val is None or (isinstance(val, (int, float)) and val >= 0), (
                    f"D3: answer_profile emitted negative employee claim: {fact}"
                )

    def test_zero_employees_preserved_in_normalize_row(self):
        """Zero is a valid employee count and must be retained."""
        from norway_company_agent.sampling import normalize_row

        row = {
            "organisasjonsnummer": "555555555",
            "navn": "Tom AS",
            "organisasjonsform.kode": "AS",
            "antallAnsatte": "0",
            "konkurs": "false",
            "underAvvikling": "false",
            "forretningsadresse.kommune": "OSLO",
            "forretningsadresse.kommunenummer": "0301",
            "naeringskode1.kode": "",
            "naeringskode1.beskrivelse": "",
            "hjemmeside": "",
            "sisteInnsendteAarsregnskap": "",
        }
        record = normalize_row(row)
        assert record["employees"] == 0, (
            f"D3 guard: zero employees suppressed; got {record['employees']!r}"
        )

    def test_nonnumeric_employees_yields_none(self):
        """Non-numeric employee strings must produce None (already existing behaviour)."""
        from norway_company_agent.sampling import normalize_row

        row = {
            "organisasjonsnummer": "666666666",
            "navn": "Bad AS",
            "organisasjonsform.kode": "AS",
            "antallAnsatte": "N/A",
            "konkurs": "false",
            "underAvvikling": "false",
            "forretningsadresse.kommune": "",
            "forretningsadresse.kommunenummer": "",
            "naeringskode1.kode": "",
            "naeringskode1.beskrivelse": "",
            "hjemmeside": "",
            "sisteInnsendteAarsregnskap": "",
        }
        record = normalize_row(row)
        assert record["employees"] is None, (
            f"D3 guard: non-numeric employees should be None; got {record['employees']!r}"
        )


# ===========================================================================
# D4 — Revenue comparison: incompatible scope / period / currency
# ===========================================================================

class TestD4RevenueComparison:
    """
    Defect: the codebase surfaces up to 3 financial records without guarding against
    cross-scope (SELSKAP vs KONSERN), cross-currency, non-12-month, or wrong-order
    period comparisons.  validate_revenue_comparison() does not exist yet.

    PRE-FIX: ImportError (function absent) on all D4 tests.
    POST-FIX: all guards return compatible=False with a reason; valid pair accepted.
    """

    def test_cross_scope_comparison_rejected(self):
        """SELSKAP vs KONSERN comparison must be withheld."""
        from norway_company_agent.official import validate_revenue_comparison

        rec_company = {
            "account_type": "SELSKAP",
            "period": {"fraOgMed": "2022-01-01", "tilOgMed": "2022-12-31"},
            "currency": "NOK",
            "revenue": 1_000_000,
        }
        rec_group = {
            "account_type": "KONSERN",
            "period": {"fraOgMed": "2021-01-01", "tilOgMed": "2021-12-31"},
            "currency": "NOK",
            "revenue": 900_000,
        }
        result = validate_revenue_comparison(rec_company, rec_group)
        assert result["compatible"] is False, (
            f"D4: SELSKAP vs KONSERN not rejected; got {result}"
        )
        assert result.get("reason"), "D4: rejection must carry a reason"

    def test_same_scope_same_currency_full_year_accepted(self):
        """Two SELSKAP NOK 12-month records in correct order must be accepted."""
        from norway_company_agent.official import validate_revenue_comparison

        rec_new = {
            "account_type": "SELSKAP",
            "period": {"fraOgMed": "2022-01-01", "tilOgMed": "2022-12-31"},
            "currency": "NOK",
            "revenue": 1_000_000,
        }
        rec_old = {
            "account_type": "SELSKAP",
            "period": {"fraOgMed": "2021-01-01", "tilOgMed": "2021-12-31"},
            "currency": "NOK",
            "revenue": 900_000,
        }
        result = validate_revenue_comparison(rec_new, rec_old)
        assert result["compatible"] is True, (
            f"D4 guard: valid comparison rejected; got {result}"
        )

    def test_cross_currency_comparison_rejected(self):
        """NOK vs USD revenue must be withheld."""
        from norway_company_agent.official import validate_revenue_comparison

        rec_nok = {
            "account_type": "SELSKAP",
            "period": {"fraOgMed": "2022-01-01", "tilOgMed": "2022-12-31"},
            "currency": "NOK",
            "revenue": 1_000_000,
        }
        rec_usd = {
            "account_type": "SELSKAP",
            "period": {"fraOgMed": "2021-01-01", "tilOgMed": "2021-12-31"},
            "currency": "USD",
            "revenue": 100_000,
        }
        result = validate_revenue_comparison(rec_nok, rec_usd)
        assert result["compatible"] is False, (
            f"D4: cross-currency not rejected; got {result}"
        )

    def test_non_twelve_month_period_rejected(self):
        """A six-month period must be flagged as incomparable."""
        from norway_company_agent.official import validate_revenue_comparison

        rec_full = {
            "account_type": "SELSKAP",
            "period": {"fraOgMed": "2022-01-01", "tilOgMed": "2022-12-31"},
            "currency": "NOK",
            "revenue": 1_000_000,
        }
        rec_stub = {
            "account_type": "SELSKAP",
            "period": {"fraOgMed": "2021-07-01", "tilOgMed": "2021-12-31"},
            "currency": "NOK",
            "revenue": 500_000,
        }
        result = validate_revenue_comparison(rec_full, rec_stub)
        assert result["compatible"] is False, (
            f"D4: non-12-month period not rejected; got {result}"
        )

    def test_wrong_period_ordering_rejected(self):
        """Records passed in wrong chronological order must be rejected."""
        from norway_company_agent.official import validate_revenue_comparison

        rec_old = {
            "account_type": "SELSKAP",
            "period": {"fraOgMed": "2021-01-01", "tilOgMed": "2021-12-31"},
            "currency": "NOK",
            "revenue": 900_000,
        }
        rec_new = {
            "account_type": "SELSKAP",
            "period": {"fraOgMed": "2022-01-01", "tilOgMed": "2022-12-31"},
            "currency": "NOK",
            "revenue": 1_000_000,
        }
        # rec_old passed as current, rec_new as previous: wrong order
        result = validate_revenue_comparison(rec_old, rec_new)
        assert result["compatible"] is False, (
            f"D4: wrong-order comparison not rejected; got {result}"
        )

    def test_missing_period_field_rejected(self):
        """A record with period=None must be withheld from comparison."""
        from norway_company_agent.official import validate_revenue_comparison

        rec_good = {
            "account_type": "SELSKAP",
            "period": {"fraOgMed": "2022-01-01", "tilOgMed": "2022-12-31"},
            "currency": "NOK",
            "revenue": 1_000_000,
        }
        rec_no_period = {
            "account_type": "SELSKAP",
            "period": None,
            "currency": "NOK",
            "revenue": 900_000,
        }
        result = validate_revenue_comparison(rec_good, rec_no_period)
        assert result["compatible"] is False, (
            f"D4: missing-period record not rejected; got {result}"
        )


# ===========================================================================
# D5 — Website quarantine: quarantined evidence surfaces as available claim
# ===========================================================================

class TestD5WebsiteQuarantine:
    """
    Defect: build_claims_and_evidence() uses availability_state() which returns
    available for any record with status=available, without checking the nested
    identity_assessment.publishable flag.  A quarantined website therefore enters
    the contract with availability=available and confidence=1.0.

    PRE-FIX: test_quarantined_website_not_available_in_claims FAILS (availability==available).
    POST-FIX: quarantined website yields availability=not_available, confidence=0.0.
    """

    def _quarantined_website_evidence(self, publishable=False):
        value: dict[str, Any] = {
            "title": "Acme Corp – Official Site",
            "description": "We deliver excellence.",
            "final_url": "https://example.com",
            "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/acme"}],
        }
        if publishable is not None:
            value["identity_assessment"] = {
                "status": "review",
                "score": 0.5,
                "publishable": publishable,
                "reasons": ["weak identity match"],
            }
        return {
            "status": "available",
            "source_url": "https://example.com",
            "source_class": "company_website",
            "retrieved_at": "2024-01-01T00:00:00+00:00",
            "content_sha256": "a" * 64,
            "value": value,
        }

    def test_quarantined_website_not_available_in_claims(self):
        """website record with publishable=False must yield availability != available."""
        from norway_company_agent.batch import build_claims_and_evidence

        profile = _make_profile()
        profile["evidence"]["website"] = self._quarantined_website_evidence(publishable=False)
        claims, _ = build_claims_and_evidence(profile, ["website"])
        website_claim = next((c for c in claims if c["field"] == "website"), None)
        assert website_claim is not None, "D5: website claim absent"
        assert website_claim["availability"] != "available", (
            f"D5: quarantined website yielded availability={website_claim['availability']!r}; "
            "expected not_available"
        )
        assert website_claim.get("confidence", 1.0) == 0.0, (
            f"D5: quarantined website has confidence={website_claim.get('confidence')}"
        )

    def test_quarantined_website_value_not_published(self):
        """Quarantined description must not appear in the claim value."""
        from norway_company_agent.batch import build_claims_and_evidence

        profile = _make_profile()
        profile["evidence"]["website"] = self._quarantined_website_evidence(publishable=False)
        claims, _ = build_claims_and_evidence(profile, ["website"])
        website_claim = next((c for c in claims if c["field"] == "website"), None)
        assert website_claim is not None
        claim_value = website_claim.get("value") or {}
        desc = (claim_value.get("description") or "") if isinstance(claim_value, dict) else str(claim_value)
        assert "excellence" not in desc, (
            "D5: quarantined description leaked into claim value"
        )

    def test_missing_identity_assessment_treated_as_quarantined(self):
        """Website evidence with no identity_assessment must not be available."""
        from norway_company_agent.batch import build_claims_and_evidence

        profile = _make_profile()
        profile["evidence"]["website"] = self._quarantined_website_evidence(publishable=None)
        claims, _ = build_claims_and_evidence(profile, ["website"])
        website_claim = next((c for c in claims if c["field"] == "website"), None)
        assert website_claim is not None
        assert website_claim["availability"] != "available", (
            f"D5: website without identity_assessment accepted as "
            f"availability={website_claim['availability']!r}"
        )

    def test_publishable_website_remains_available(self):
        """A website with publishable=True must remain availability=available."""
        from norway_company_agent.batch import build_claims_and_evidence

        profile = _make_profile()
        ev = self._quarantined_website_evidence(publishable=True)
        ev["value"]["identity_assessment"]["status"] = "exact"
        ev["value"]["identity_assessment"]["score"] = 1.0
        profile["evidence"]["website"] = ev
        claims, _ = build_claims_and_evidence(profile, ["website"])
        website_claim = next((c for c in claims if c["field"] == "website"), None)
        assert website_claim is not None
        assert website_claim["availability"] == "available", (
            f"D5 guard: publishable website blocked; got {website_claim['availability']!r}"
        )

    def test_research_withholds_quarantined_description(self):
        """answer_profile must not emit quarantined website description as a fact."""
        from norway_company_agent.research import answer_profile

        profile = _make_profile()
        profile["evidence"]["website"] = self._quarantined_website_evidence(publishable=False)
        result = answer_profile(profile, "Describe this company")
        descriptions = [f["value"] for f in result["facts"] if "description" in f["claim"].lower()]
        assert not descriptions, (
            f"D5: answer_profile emitted quarantined description: {descriptions}"
        )


# ===========================================================================
# Task 1 — D1 Status Hardening Regression Tests
# ===========================================================================

class TestD1StatusHardening:
    """Explicit and conservative status interpretation tests."""

    def test_missing_status_fields_classified_unknown(self):
        """A row missing konkurs/underAvvikling must evaluate to state=unknown and not eligible."""
        from norway_company_agent.sampling import normalize_row, stratum, financial_filer_eligible

        row = {
            "organisasjonsnummer": "123456789",
            "navn": "Missing Status AS",
            "organisasjonsform.kode": "AS",
            "sisteInnsendteAarsregnskap": "2023",
        }
        rec = normalize_row(row)
        s = stratum(rec)
        state_part = s.split("|")[2]
        assert state_part == "unknown", f"Expected state=unknown, got {state_part!r}"
        assert financial_filer_eligible(rec, "2023", active_only=True) is False

    def test_none_status_fields_classified_unknown(self):
        """A row with None status fields must evaluate to state=unknown and not eligible."""
        from norway_company_agent.sampling import normalize_row, stratum, financial_filer_eligible

        row = {
            "organisasjonsnummer": "123456789",
            "navn": "None Status AS",
            "organisasjonsform.kode": "AS",
            "konkurs": None,
            "underAvvikling": None,
            "slettedato": None,
            "underTvangsavvikling": None,
            "sisteInnsendteAarsregnskap": "2023",
        }
        rec = normalize_row(row)
        s = stratum(rec)
        state_part = s.split("|")[2]
        assert state_part == "unknown", f"Expected state=unknown, got {state_part!r}"
        assert financial_filer_eligible(rec, "2023", active_only=True) is False

    def test_empty_string_status_classified_unknown(self):
        """A row with empty string status flags must evaluate to state=unknown."""
        from norway_company_agent.sampling import normalize_row, stratum, financial_filer_eligible

        row = {
            "organisasjonsnummer": "123456789",
            "navn": "Empty Status AS",
            "organisasjonsform.kode": "AS",
            "konkurs": "",
            "underAvvikling": "",
            "sisteInnsendteAarsregnskap": "2023",
        }
        rec = normalize_row(row)
        s = stratum(rec)
        state_part = s.split("|")[2]
        assert state_part == "unknown", f"Expected state=unknown, got {state_part!r}"
        assert financial_filer_eligible(rec, "2023", active_only=True) is False

    def test_malformed_string_status_classified_unknown(self):
        """Unrecognized status strings like 'maybe' must evaluate to state=unknown."""
        from norway_company_agent.sampling import normalize_row, stratum, financial_filer_eligible

        row = {
            "organisasjonsnummer": "123456789",
            "navn": "Malformed Status AS",
            "organisasjonsform.kode": "AS",
            "konkurs": "maybe",
            "underAvvikling": "unknown",
            "sisteInnsendteAarsregnskap": "2023",
        }
        rec = normalize_row(row)
        s = stratum(rec)
        state_part = s.split("|")[2]
        assert state_part == "unknown", f"Expected state=unknown, got {state_part!r}"
        assert financial_filer_eligible(rec, "2023", active_only=True) is False

    def test_documented_truthy_encodings_bankrupt_classified_adverse(self):
        """Documented true encodings ('1', 'ja', 'yes', 'j') must classify company as adverse."""
        from norway_company_agent.sampling import normalize_row, stratum, financial_filer_eligible

        for enc in ["1", "ja", "yes", "j", "true", "True"]:
            row = {
                "organisasjonsnummer": "123456789",
                "navn": "Bankrupt Enc AS",
                "organisasjonsform.kode": "AS",
                "konkurs": enc,
                "underAvvikling": "false",
                "sisteInnsendteAarsregnskap": "2023",
            }
            rec = normalize_row(row)
            s = stratum(rec)
            state_part = s.split("|")[2]
            assert state_part == "adverse", f"For konkurs={enc!r}, expected adverse, got {state_part!r}"
            assert financial_filer_eligible(rec, "2023", active_only=True) is False

    def test_documented_falsy_encodings_classified_active(self):
        """Documented falsy encodings ('0', 'nei', 'no', 'n', 'false') must classify confirmed active."""
        from norway_company_agent.sampling import normalize_row, stratum, financial_filer_eligible

        for enc in ["0", "nei", "no", "n", "false", "False"]:
            row = {
                "organisasjonsnummer": "123456789",
                "navn": "Active Enc AS",
                "organisasjonsform.kode": "AS",
                "konkurs": enc,
                "underAvvikling": enc,
                "underTvangsavvikling": enc,
                "slettedato": "",
                "sisteInnsendteAarsregnskap": "2023",
            }
            rec = normalize_row(row)
            s = stratum(rec)
            state_part = s.split("|")[2]
            assert state_part == "active", f"For status={enc!r}, expected active, got {state_part!r}"
            assert financial_filer_eligible(rec, "2023", active_only=True) is True

    def test_whitespace_deletion_date_not_active(self):
        """Whitespace in slettedato must not classify company as active."""
        from norway_company_agent.sampling import normalize_row, stratum, financial_filer_eligible

        row = {
            "organisasjonsnummer": "123456789",
            "navn": "Whitespace Slettet AS",
            "organisasjonsform.kode": "AS",
            "konkurs": "false",
            "underAvvikling": "false",
            "slettedato": "   ",
            "sisteInnsendteAarsregnskap": "2023",
        }
        rec = normalize_row(row)
        s = stratum(rec)
        state_part = s.split("|")[2]
        assert state_part != "active", f"Whitespace slettedato classified as active: {s}"
        assert financial_filer_eligible(rec, "2023", active_only=True) is False


# ===========================================================================
# Task 2 — D3 Employee Count Hardening Regression Tests
# ===========================================================================

class TestD3EmployeeHardening:
    """Boundary-level employee count parsing and leak prevention tests."""

    def test_string_negative_employees_normalize_row_rejects(self):
        """normalize_row must map negative numeric string '-5' to None."""
        from norway_company_agent.sampling import normalize_row

        row = {"organisasjonsnummer": "123456789", "antallAnsatte": "-5"}
        rec = normalize_row(row)
        assert rec["employees"] is None

    def test_research_answer_rejects_string_negative_employees(self):
        """answer_profile must NOT emit a fact when employees is a string '-5'."""
        from norway_company_agent.research import answer_profile

        profile = _make_profile(employees="-5")
        result = answer_profile(profile, "How many employees does this company have?")
        for fact in result["facts"]:
            assert "employee" not in fact["claim"].lower(), f"Leaked negative employee fact: {fact}"

    def test_research_answer_rejects_malformed_string_employees(self):
        """answer_profile must NOT emit facts for malformed values like 'five' or '3.5'."""
        from norway_company_agent.research import answer_profile

        for bad in ["five", "3.5", "-1", "N/A", "--", "   "]:
            profile = _make_profile(employees=bad)
            result = answer_profile(profile, "employees")
            emp_facts = [f for f in result["facts"] if "employee" in f["claim"].lower()]
            assert not emp_facts, f"Malformed employee {bad!r} emitted into facts: {emp_facts}"

    def test_research_answer_preserves_zero_and_positive_integers(self):
        """answer_profile must preserve integer 0, string '0', and positive integers."""
        from norway_company_agent.research import answer_profile

        for valid_val, expected in [(0, 0), ("0", 0), (15, 15), ("15", 15)]:
            profile = _make_profile(employees=valid_val)
            result = answer_profile(profile, "employees")
            emp_facts = [f for f in result["facts"] if "employee" in f["claim"].lower()]
            assert len(emp_facts) == 1, f"Expected 1 fact for {valid_val!r}, got {emp_facts}"
            assert emp_facts[0]["value"] == expected, f"Expected {expected}, got {emp_facts[0]['value']}"
