from __future__ import annotations

import json
import gzip
import csv
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.crawl_events import extract_page_event, merge_profile_events, missing_seed_error_events  # noqa: E402
from norway_company_agent.discovery import build_company_search_query, choose_search_candidate, parse_brave_web_results, score_search_candidate  # noqa: E402
from norway_company_agent.official import _reserve_history_slot, accounting_obligation_assessment, normalize_entity, normalize_financial_history, normalize_financials, normalize_roles  # noqa: E402
from norway_company_agent.operations import domain_request_summary, latency_summary, percentile  # noqa: E402
from norway_company_agent.sampling import deterministic_extension_sample, deterministic_financial_filer_sample, deterministic_website_audit_sample, financial_filer_eligible, normalize_row, stratum  # noqa: E402
from norway_company_agent.research import answer_profile, parse_screen_query, screen_profiles  # noqa: E402
from norway_company_agent.workspace import load_workspace, record_screen, save_workspace  # noqa: E402
from norway_company_agent.refresh import diff_datasets, diff_profile  # noqa: E402
from norway_company_agent.sentiment import aggregate_company_sentiment, evaluate_predictions, publishable_sentiment_item, sentiment_input_eligibility  # noqa: E402
from norway_company_agent.external_footprint import (  # noqa: E402
    _is_organisation_number,
    aggregate_footprint,
    diagnostic_id,
    is_organisation_number,
    observation_fingerprint,
    parse_timestamp,
    publishable_observation,
    validate_observation,
)
from norway_company_agent.external_pipeline import (  # noqa: E402
    ObservationInputError,
    audit_records,
    company_external_block,
    coverage_from_observations,
    deduplicate_observations,
    eligible_observations,
    gate_observations,
    read_observation_file,
    run_external_summary,
)
from norway_company_agent.external_tasks import plan_external_tasks  # noqa: E402
from norway_company_agent.external_control import development_score, run_company_control, strategy_order  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate, assess_social_identity, assess_website_identity  # noqa: E402
from norway_company_agent.website import _extraction_state, _priority_links, _social_links, assert_public_url, normalize_homepage, normalize_social_url, structured_social_links  # noqa: E402
from norway_company_agent.batch import (  # noqa: E402
    AVAILABILITY_STATES,
    CONTRACT_VERSION,
    EVIDENCE_STATUS_TO_AVAILABILITY,
    availability_state,
    contract_envelope,
    evidence_terminal_state,
    profile_complete_for_modules,
    read_organisation_inputs,
    terminal_envelope,
    validate_contract_envelope,
    validate_envelopes,
)
from norway_company_agent.snapshots import SnapshotFetcher  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from scripts.build_prototype import compact as compact_prototype, qualification_copy  # noqa: E402
from scripts.run_brave_discovery import brave_search  # noqa: E402
from scripts.run_annual_report_workforce_connector import extract_candidate, needs_ocr  # noqa: E402
from scripts.normalize_google_maps_results import candidate_score  # noqa: E402
from scripts.run_scrapy_websites import terminal_events_for_run  # noqa: E402
from scripts.run_sentiment_model import MODEL_REVISION, normalize_generated_label  # noqa: E402
from scripts.score_company_completeness import score_rows, summarize  # noqa: E402
from scripts.score_competition_v3 import capped, numeric  # noqa: E402
from scripts.extract_company_site_activity import observation as site_activity_observation  # noqa: E402
from scripts.extract_company_site_news import observation as site_news_observation  # noqa: E402
from scripts.build_verified_observations import build as build_verified_observations  # noqa: E402
from scripts.run_google_news_rss_connector import exact_title_match  # noqa: E402
from scripts.run_linkedin_guest_jobs_connector import canonical_company_url, parse_detail_company_urls, parse_job_cards, parse_typeahead  # noqa: E402
from scripts.run_linkedin_guest_experiment import (  # noqa: E402
    assess_profile_identity as assess_linkedin_profile_identity,
    extract_profile as extract_linkedin_profile,
    legal_name_profile_url,
)
from scripts.run_fagfolkguiden_reviews_connector import extract_aggregate_rating, slug  # noqa: E402
from scripts.discover_linkedin_company_profiles import (  # noqa: E402
    discovery_identity as linkedin_discovery_identity,
    normalized_full_name as linkedin_normalized_full_name,
    official_site_aliases as linkedin_official_site_aliases,
    parse_exact_typeahead as parse_linkedin_exact_typeahead,
)


class EvidenceTests(unittest.TestCase):
    def test_missing_is_not_zero_and_provenance_is_required(self):
        record = evidence("financials", "not_found", "official_annual_accounts", "https://example.test/123")
        self.assertIsNone(record["value"])
        self.assertNotEqual(record["value"], 0)
        self.assertTrue(record["source_url"])
        self.assertTrue(record["retrieved_at"])

    def test_available_zero_is_preserved(self):
        record = evidence("employees", "available", "official_registry_bulk", "https://example.test", value=0)
        self.assertEqual(record["value"], 0)
        self.assertEqual(record["status"], "available")

    def test_content_hash_can_be_carried_with_evidence(self):
        record = evidence("entity", "available", "official", "https://example.test", value={}, content_sha256="a" * 64, source_row_key="999999999")
        self.assertEqual(record["content_sha256"], "a" * 64)
        self.assertEqual(record["source_class"], "official")
        self.assertEqual(record["source_row_key"], "999999999")

    def test_not_fetched_is_distinct_from_not_applicable(self):
        record = evidence("history", "not_fetched", "official", "https://example.test", note="No filing flag in snapshot")
        self.assertEqual(record["status"], "not_fetched")
        self.assertNotEqual(record["status"], "not_applicable")


class ExternalFootprintTests(unittest.TestCase):
    def observation(self, **changes):
        base = {
            "id": "obs-1",
            "organisation_number": "923609016",
            "platform": "google_places",
            "signal_type": "review",
            "source_url": "https://maps.google.com/example",
            "retrieved_at": "2026-08-20T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "address_match", "value": "Oslo"}],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "customer_review",
            "evidence_span": "Helpful staff",
        }
        return {**base, **changes}

    def test_publication_requires_rights_identity_hash_and_span(self):
        self.assertTrue(publishable_observation(self.observation()))
        bad = self.observation(exact_entity=False, content_sha256=None, evidence_span=None, rights_status="unknown")
        reasons = validate_observation(bad)
        self.assertIn("exact legal entity is not verified", reasons)
        self.assertIn("missing content hash", reasons)
        self.assertIn("missing evidence span", reasons)
        self.assertIn("source rights are not approved", reasons)

    def test_unofficial_scraper_output_is_experimental_not_publishable(self):
        item = self.observation(platform="linkedin", signal_type="job_posting", acquisition_mode="jobspy_experiment")
        self.assertFalse(publishable_observation(item))

    def test_linkedin_guest_jobs_require_exact_verified_company_url(self):
        raw = b'''<div class="base-search-card" data-entity-urn="urn:li:jobPosting:4456746433">
          <a class="base-card__full-link" href="https://no.linkedin.com/jobs/view/example-4456746433?x=1"></a>
          <span class="sr-only">Project manager</span>
          <h4 class="base-search-card__subtitle"><a href="https://no.linkedin.com/company/af-gruppen?trk=x">AF Gruppen</a></h4>
          <span class="job-search-card__location">Oslo</span><time datetime="2026-08-23"></time>
        </div>
        <div class="base-search-card" data-entity-urn="urn:li:jobPosting:4456746434">
          <a class="base-card__full-link" href="https://linkedin.com/jobs/view/other-4456746434"></a>
          <span class="sr-only">Wrong parent job</span>
          <h4 class="base-search-card__subtitle"><a href="https://linkedin.com/company/af-gruppen-sverige">AF Gruppen Sverige</a></h4>
        </div>'''
        jobs, candidates = parse_job_cards(raw, "https://linkedin.com/company/af-gruppen")
        self.assertEqual(candidates, 2)
        self.assertEqual([item["job_id"] for item in jobs], ["4456746433"])
        self.assertEqual(jobs[0]["company_url"], "https://linkedin.com/company/af-gruppen")

    def test_linkedin_company_urls_and_typeahead_are_normalized_without_claiming_ambiguous_ids(self):
        self.assertEqual(
            canonical_company_url("https://no.linkedin.com/company/Norsk-Fiskeeksport/about?trk=x"),
            "https://linkedin.com/company/norsk-fiskeeksport",
        )
        candidates = parse_typeahead(
            json.dumps([
                {"id": "34440", "type": "COMPANY", "displayName": "AF Gruppen"},
                {"id": "1188022", "type": "COMPANY", "displayName": "AF Gruppen Sverige"},
            ]).encode(),
            "AF GRUPPEN ASA",
        )
        self.assertTrue(candidates[0]["exact_legal_name_core"])
        self.assertFalse(candidates[1]["exact_legal_name_core"])
        self.assertEqual(
            parse_detail_company_urls(
                b'<a href="https://no.linkedin.com/company/af-gruppen?trk=job">AF Gruppen</a>'
                b'<a href="https://example.test/company/wrong">Wrong</a>'
            ),
            {"https://linkedin.com/company/af-gruppen"},
        )

    def test_linkedin_guest_profile_uses_structured_company_data_and_ignores_dormant_challenge_code(self):
        graph = {
            "@graph": [
                {
                    "@type": "DiscussionForumPosting",
                    "author": {"url": "https://no.linkedin.com/company/af-gruppen"},
                    "datePublished": "2026-08-21T06:15:05Z",
                    "text": "Exact company update",
                    "url": "https://no.linkedin.com/posts/example-activity-7496449781678927873-x",
                },
                {
                    "@type": "Organization",
                    "name": "AF Gruppen",
                    "url": "https://no.linkedin.com/company/af-gruppen",
                    "description": "Construction group",
                    "numberOfEmployees": {"value": 1303},
                },
            ]
        }
        raw = (
            '<meta name="description" content="AF Gruppen | 56 726 followers on LinkedIn">'
            f'<script type="application/ld+json">{json.dumps(graph)}</script>'
            '<script>const dormant="recaptcha/challengepage";</script>'
            '<div data-test-id="about-us__size"><dd>5,001-10,000 employees</dd></div>'
            '<article class="main-feed-activity-card" data-activity-urn="urn:li:activity:7496449781678927873">'
            '<a data-test-id="social-actions__reactions" data-num-reactions="29"></a>'
            '<a data-test-id="social-actions__comments" data-num-comments="4"></a></article>'
        ).encode()
        profile = extract_linkedin_profile(raw, "https://linkedin.com/company/af-gruppen")
        self.assertEqual(profile["followers"], 56726)
        self.assertEqual(profile["visible_employees"], 1303)
        self.assertEqual(profile["employee_size_label"], "5,001-10,000 employees")
        self.assertEqual(profile["posts"][0]["likes"], 29)
        self.assertEqual(profile["posts"][0]["comments"], 4)

    def test_linkedin_guest_profile_rejects_authwall_without_organization_data(self):
        with self.assertRaisesRegex(RuntimeError, "no structured organization"):
            extract_linkedin_profile(b'<script>recaptcha/challengepage</script>', "https://linkedin.com/company/example")

    def test_linkedin_stale_handle_fallback_is_bounded_to_registry_legal_name(self):
        self.assertEqual(legal_name_profile_url("DIPS AS"), "https://www.linkedin.com/company/dips-as")
        self.assertEqual(legal_name_profile_url("RØD & BLÅ AS"), "https://www.linkedin.com/company/rod-bla-as")

    def test_linkedin_discovery_requires_exact_typeahead_name_and_corroboration(self):
        raw = json.dumps([
            {"id": "1", "type": "COMPANY", "displayName": "DIPS AS"},
            {"id": "2", "type": "COMPANY", "displayName": "DIPS ASA"},
        ]).encode()
        self.assertEqual([item["linkedin_company_id"] for item in parse_linkedin_exact_typeahead(raw, "DIPS AS")], ["1"])
        self.assertEqual(linkedin_normalized_full_name("RØD & BLÅ AS"), "rød blå as")
        company = {
            "name": "DIPS AS",
            "municipality": "BODØ",
            "website": "https://dips.com",
            "evidence": {"website": {"status": "available", "value": {"final_url": "https://dips.com"}}},
        }
        exact = linkedin_discovery_identity(company, {"name": "DIPS AS", "website": "https://www.dips.com", "headquarters": "Bodø"}, {"legal_name_slug"})
        self.assertTrue(exact["exact_entity"])
        weak = linkedin_discovery_identity(company, {"name": "DIPS AS", "website": "https://unrelated.test", "headquarters": "Oslo"}, {"legal_name_slug"})
        self.assertFalse(weak["exact_entity"])

    def test_linkedin_fuzzy_discovery_uses_verified_site_alias_and_reverse_domain(self):
        company = {
            "name": "JARRE AS",
            "municipality": "INDRE ØSTFOLD",
            "website": "https://jarre.co",
            "evidence": {
                "website": {"status": "available", "value": {"final_url": "https://jarre.co", "title": "Jarre&Co"}},
                "roles": {"value": {"roles": [{"name": "Christian Jarre", "role_code": "DAGL"}]}},
            },
        }
        self.assertEqual(linkedin_official_site_aliases(company), ["Jarre&Co"])
        exact = linkedin_discovery_identity(
            company,
            {"name": "Jarre & Co", "website": "https://www.jarre.co", "headquarters": "Askim", "description": ""},
            {"official_site_alias:Jarre&Co"},
        )
        self.assertTrue(exact["exact_entity"])

    def test_linkedin_profile_identity_accepts_redirect_alias_only_with_name_or_reverse_domain_proof(self):
        profile = {
            "name": "ZAPTEC ASA",
            "website": "https://zaptec.com",
            "evidence": {"website": {"source_url": "https://www.zaptec.com/", "value": {"final_url": "https://www.zaptec.com/"}}},
        }
        accepted = assess_linkedin_profile_identity(
            profile,
            "https://linkedin.com/company/gozaptec",
            {"name": "Zaptec", "page_url": "https://linkedin.com/company/zaptec", "website": "https://www.zaptec.com"},
        )
        self.assertTrue(accepted["publishable_candidate"])
        rejected = assess_linkedin_profile_identity(
            profile,
            "https://linkedin.com/company/gozaptec",
            {"name": "Unrelated Parent", "page_url": "https://linkedin.com/company/unrelated", "website": "https://parent.test"},
        )
        self.assertFalse(rejected["publishable_candidate"])

    def test_google_play_observation_is_supported_but_unofficial_output_stays_experimental(self):
        item = self.observation(
            platform="google_play",
            signal_type="review_summary",
            acquisition_mode="unofficial_api_experiment",
            rights_status="review_required",
        )
        reasons = validate_observation(item)
        self.assertNotIn("unsupported platform", reasons)
        self.assertFalse(publishable_observation(item))

    def test_company_directory_is_not_a_website_discovery_candidate(self):
        profile = {"name": "OBLOMOV AS", "organisation_number": "991167315", "municipality": "SOLA"}
        result = {"url": "https://www.northdata.com/Oblomov-AS/BR-991167315", "title": "Oblomov AS", "snippet": "991167315", "rank": 1}
        assessment = score_search_candidate(profile, result)
        self.assertFalse(assessment["publishable_candidate"])
        self.assertEqual(assessment["status"], "rejected")

    def test_unknown_company_directory_with_org_number_is_not_a_candidate(self):
        profile = {"name": "AKSLA AS", "organisation_number": "923304290", "municipality": "ÅLESUND"}
        result = {"url": "https://vexter.no/selskap/aksla-as/923304290", "title": "AKSLA AS", "snippet": "923304290", "rank": 1}
        self.assertFalse(score_search_candidate(profile, result)["publishable_candidate"])

    def test_annual_workforce_parser_does_not_treat_norwegian_o_as_zero(self):
        heading = "Note 2 - Lonnskostnader, antall ansatte og lan til ansatte"
        self.assertEqual(extract_candidate(heading), (None, None, "no_employee_phrase", None))
        count, span, status, measure = extract_candidate("Det er to ansatte i sameiet.")
        self.assertEqual((count, status, measure), (2, "accepted", "employees"))
        self.assertEqual(span, "Det er to ansatte i sameiet.")
        self.assertEqual(extract_candidate("Selskapet har 1 2025 sysselsatt 2 arsverk.")[0], 2)
        self.assertEqual(extract_candidate("Antall arsverk syssetsatt i regnskapsaret: 3")[0], 3)
        self.assertEqual(extract_candidate("Stiftelsen har ingen ansatte og ingen arsverk.")[0], 0)
        self.assertEqual(extract_candidate("Selskapet hadde ingen ansatte i 2025.")[0], 0)
        self.assertEqual(extract_candidate("Gjennomsnittlig antall ansatte i regnskapsaret: 0")[0], 0)
        self.assertEqual(extract_candidate("Note Antall Aarsverk i regnskapsaret 0.00")[0], 0)
        self.assertEqual(extract_candidate("Tal pa Aarsverk i rekneskapsaret 1.50")[0], 1.5)
        self.assertTrue(needs_ocr("Digital cover text without the employee note"))
        self.assertFalse(needs_ocr("Selskapet har 2 ansatte. " + "Digital report text. " * 8))

    def test_aggregate_keeps_source_metrics_separate_and_abstains_on_thin_sentiment(self):
        items = [
            self.observation(id="a", sentiment_label="positive", sentiment_model_version="m1"),
            self.observation(id="b", platform="youtube", signal_type="profile_metrics", source_url="https://youtube.com/@example", evidence_span=None),
        ]
        result = aggregate_footprint(items, as_of="2026-08-22T00:00:00Z")
        self.assertEqual(result["accepted_observations"], 2)
        self.assertEqual(result["sentiment"]["status"], "abstain")
        self.assertNotIn("popularity_score", result)

    def test_customer_review_sentiment_accepts_ten_independent_reviewers_on_one_platform(self):
        items = [
            self.observation(
                id=f"review-{index}",
                sentiment_label="positive",
                sentiment_model_version="explicit_star_rating_v1",
                reviewer_id=f"reviewer-{index}",
            )
            for index in range(10)
        ]
        result = aggregate_footprint(items, as_of="2026-08-22T00:00:00Z")
        self.assertEqual(result["sentiment"]["status"], "available")
        self.assertEqual(result["sentiment"]["independent_reviewers"], 10)

    def test_google_maps_identity_gate_rejects_neighbor_and_accepts_exact_address(self):
        profile = {
            "organisation_number": "938702675",
            "name": "AF GRUPPEN ASA",
            "evidence": {
                "registry": {"value": {
                    "forretningsadresse.adresse": "Standardveien 1",
                    "forretningsadresse.postnummer": "0581",
                    "telefon": "22 89 11 00",
                }},
                "website": {"value": {
                    "final_url": "https://afgruppen.no/",
                    "identity_assessment": {"publishable": True},
                }},
            },
        }
        exact = candidate_score(profile, {
            "title": "AF Gruppen", "address": "Standardveien 1, 0581 Oslo, Norge",
            "phone": "+47 22 89 11 00", "web_site": "https://afgruppen.no/", "review_count": 21,
        })
        neighbor = candidate_score(profile, {
            "title": "AF Eiendom", "address": "Standardveien 1, 0581 Oslo, Norge",
            "phone": "+47 22 89 11 00", "web_site": "https://afgruppen.no/eiendom/", "review_count": 0,
        })
        self.assertTrue(exact["accepted"])
        self.assertFalse(neighbor["accepted"])

    def test_google_maps_exact_name_and_postcode_city_can_resolve_operating_address(self):
        profile = {
            "organisation_number": "999999999",
            "name": "EXAMPLE INDUSTRI AS",
            "evidence": {"registry": {"value": {
                "forretningsadresse.adresse": "c/o Accountant Other Street 1",
                "forretningsadresse.postnummer": "4021",
                "forretningsadresse.poststed": "STAVANGER",
            }}},
        }
        result = candidate_score(profile, {
            "title": "Example Industri AS", "address": "Factory Road 7, 4021 Stavanger, Norway",
            "phone": "", "web_site": "", "review_count": 4,
        })
        self.assertTrue(result["accepted"])
        self.assertTrue(result["postcode_city_match"])

    def test_google_maps_trade_name_requires_exact_address_phone_and_no_partial_name_collision(self):
        profile = {
            "name": "OSLOFJORDEN EIENDOMSMEGLING AS",
            "evidence": {"registry": {"value": {
                "forretningsadresse.adresse": "Stranden 81", "forretningsadresse.postnummer": "0250",
                "forretningsadresse.poststed": "Oslo", "telefon": "22620000",
            }}},
        }
        candidate = {"title": "PrivatMegleren Premium", "address": "Stranden 81, 0250 Oslo", "phone": "+47 22 62 00 00"}
        result = candidate_score(profile, candidate)
        self.assertFalse(result["trade_name_match"])
        self.assertFalse(result["accepted"])
        profile["organisation_number"] = "932083108"
        result = candidate_score(profile, candidate)
        self.assertTrue(result["trade_name_match"])
        self.assertTrue(result["accepted"])
        candidate["phone"] = "+47 99 99 99 99"
        self.assertFalse(candidate_score(profile, candidate)["accepted"])

    def test_experimental_maps_signals_raise_only_experimental_places_score(self):
        profile = {
            "organisation_number": "938702675",
            "name": "AF GRUPPEN ASA",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": False}}}},
        }
        common = {
            "organisation_number": "938702675",
            "platform": "google_places",
            "source_url": "https://www.google.com/maps/place/example",
            "retrieved_at": "2026-08-22T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "registry_address_match", "value": True}],
            "acquisition_mode": "unofficial_api_experiment",
            "rights_status": "review_required",
            "source_class": "public_business_listing",
            "evidence_span": "AF Gruppen; Standardveien 1; rating=2.5; reviews=21",
        }
        observations = [
            {**common, "id": "place", "signal_type": "place_summary", "strategy": "places_identity_resolution"},
            {**common, "id": "summary", "signal_type": "review_summary", "strategy": "places_rating_reviews"},
        ]
        score = development_score(profile, observations)
        self.assertEqual(score["score"], 0.0)
        self.assertEqual(score["experimental_potential_score"], 35.0)

    def test_aggregate_maps_rating_is_experimental_sentiment_and_buzz(self):
        profile = {
            "organisation_number": "938702675",
            "name": "AF GRUPPEN ASA",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": False}}}},
        }
        common = {
            "organisation_number": "938702675",
            "platform": "google_places",
            "source_url": "https://www.google.com/maps/place/example",
            "retrieved_at": "2026-08-22T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "registry_address_match", "value": True}],
            "acquisition_mode": "unofficial_api_experiment",
            "rights_status": "review_required",
            "evidence_span": "AF Gruppen; rating=4.4; reviews=21",
            "metrics": {"rating": 4.4, "rating_scale": 5, "review_count": 21},
        }
        observations = [
            {**common, "id": "summary", "signal_type": "review_summary", "strategy": "places_rating_reviews"},
            {**common, "id": "buzz", "signal_type": "buzz_metrics", "strategy": "buzz_peer_normalization"},
        ]
        score = development_score(profile, observations)
        self.assertEqual(score["score"], 0.0)
        self.assertEqual(score["experimental_sentiment_status"], "available")
        self.assertEqual(score["experimental_potential_score"], 35.0)

    def test_task_planner_uses_verified_handles_and_adds_core_connectors(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {
                "website": {"value": {"social_links": [{"platform": "youtube", "url": "https://youtube.com/@example"}]}},
            },
        }
        tasks = plan_external_tasks(profile)
        connectors = {item["connector"] for item in tasks}
        self.assertIn("google_places_api", connectors)
        self.assertIn("jobs_provider", connectors)
        self.assertIn("youtube_connector", connectors)
        self.assertNotIn("permitted_search_api", connectors)

    def test_controller_recomputes_sentiment_and_records_marginal_gain(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": True}}}},
        }
        handle = self.observation(signal_type="profile_handle", source_class="company_social", evidence_span=None, strategy="verified_handle_extraction")
        score = development_score(profile, [handle])
        self.assertGreater(score["score"], 0)
        self.assertEqual(score["sentiment_status"], "abstain")
        result = run_company_control(profile, [handle], minimum_iterations=10, maximum_iterations=15)
        self.assertGreaterEqual(result["iterations_run"], 10)
        self.assertTrue(any(item["score_delta"] > 0 for item in result["iterations"]))
        self.assertTrue(all("sentiment_status" in item for item in result["iterations"]))

    def test_controller_final_score_is_not_path_dependent_after_target_is_reached(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": True}}}},
        }
        observations = [
            self.observation(signal_type="profile_handle", source_class="company_social", evidence_span=None, strategy="verified_handle_extraction"),
            self.observation(id="metric", platform="youtube", signal_type="profile_metrics", source_url="https://youtube.com/@example", evidence_span=None, strategy="social_profile_metrics"),
        ]
        result = run_company_control(profile, observations, target=20, minimum_iterations=1)
        self.assertEqual(result["iterations_run"], len(strategy_order([])))
        self.assertEqual(result["final"], development_score(profile, observations))

    def test_exact_wikidata_org_profile_can_supply_external_identity(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": False}}}},
        }
        wikidata = self.observation(
            platform="wikidata",
            signal_type="company_profile",
            source_url="https://www.wikidata.org/wiki/Q123",
            evidence_span="Q123: P2333=923609016",
            source_class="open_knowledge_graph",
            strategy="company_site_identity",
        )
        score = development_score(profile, [wikidata])
        self.assertEqual(score["components"]["exact_external_identity"], 20.0)
        self.assertTrue(publishable_observation(wikidata))

    def test_controller_replicates_prior_winning_strategy_first(self):
        prior = [
            {"strategy": "youtube_channel_feed", "learning_gain": 8.0},
            {"strategy": "verified_handle_extraction", "learning_gain": 2.0},
        ]
        self.assertEqual(strategy_order(prior)[0], "youtube_channel_feed")
        profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {"website": {"value": {"identity_assessment": {"publishable": True}}}}}
        result = run_company_control(profile, [], prior_iterations=prior, minimum_iterations=1, maximum_iterations=2)
        self.assertEqual(result["iterations"][0]["strategy"], "youtube_channel_feed")
        self.assertEqual(result["iterations"][0]["controller_action"], "replicate")


class CompletenessScoreTests(unittest.TestCase):
    def test_all_source_weights_sum_to_one_hundred(self):
        from scripts.score_company_completeness import ENRICHMENT_WEIGHTS, FOUNDATION_WEIGHTS

        self.assertEqual(sum(FOUNDATION_WEIGHTS.values()) + sum(ENRICHMENT_WEIGHTS.values()), 100.0)

    def test_all_source_score_combines_foundation_and_external_without_imputing_missing(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {
                "registry_live": {"status": "available", "value": {"organisation_number": "923609016"}},
                "financials": {"status": "available"},
                "roles": {"status": "available"},
                "locations": {"status": "available"},
                "website": {"status": "not_found"},
            },
        }
        components = {
            "exact_external_identity": 20,
            "verified_handles": 15,
            "profile_metrics": 10,
            "places_identity": 5,
            "places_reviews": 10,
            "workforce_jobs": 10,
            "public_buzz": 10,
            "independent_sentiment": 0,
            "freshness_evidence": 5,
        }
        result = {"organisation_number": "923609016", "company_name": "Example AS", "final": {"components": components, "experimental_components": components}}
        scored = score_rows([profile], [result])
        self.assertEqual(scored[0]["foundation_score"], 30.0)
        self.assertEqual(scored[0]["strict_enrichment"]["independent_sentiment"], 0.0)
        self.assertEqual(scored[0]["strict_completeness_score"], 88.0)
        self.assertEqual(summarize(scored)["companies"], 1)

    def test_site_activity_requires_exact_identity_and_preserves_snapshot_provenance(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {"website": {
                "status": "available",
                "source_url": "https://example.test/",
                "retrieved_at": "2026-08-23T00:00:00Z",
                "value": {
                    "final_url": "https://example.test/",
                    "content_sha256": "a" * 64,
                    "identity_assessment": {"publishable": True, "status": "exact", "score": 1.0},
                    "pages": [{"url": "https://example.test/"}],
                },
            }},
        }
        item = site_activity_observation(profile)
        self.assertIsNotNone(item)
        self.assertEqual(item["strategy"], "company_site_activity")
        self.assertTrue(publishable_observation(item))
        profile["evidence"]["website"]["value"]["identity_assessment"]["publishable"] = False
        self.assertIsNone(site_activity_observation(profile))

    def test_news_title_gate_requires_the_full_legal_name_core(self):
        self.assertTrue(exact_title_match("NORDIC DOOR AS", "Nordic Door AS åpner ny fabrikk - Lokalavisa"))
        self.assertFalse(exact_title_match("NORDIC DOOR AS", "Nordic investors prefer another door - Example"))
        self.assertTrue(exact_title_match("SOLVANG ASA", "Sterkt årsresultat fra Solvang ASA i 2024 - Skipsrevyen"))
        self.assertFalse(exact_title_match("VIND HOLDING AS", "Inntektene til Aneo Roan Vind Holding AS stupte - mn24.no"))
        self.assertFalse(exact_title_match("CONSTO AS", "Drastisk fall hos Consto Bergen AS - BT"))

    def test_site_news_requires_exact_identity_and_a_captured_news_path(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {"website": {
                "status": "available", "retrieved_at": "2026-08-23T00:00:00Z",
                "value": {
                    "identity_assessment": {"publishable": True, "score": 1.0},
                    "pages": [{"url": "https://example.test/aktuelt/new-contract", "title": "New contract", "content_sha256": "a" * 64}],
                },
            }},
        }
        item = site_news_observation(profile)
        self.assertEqual(item["signal_type"], "public_post")
        self.assertTrue(publishable_observation(item))
        profile["evidence"]["website"]["value"]["pages"][0]["url"] = "https://example.test/contact"
        self.assertIsNone(site_news_observation(profile))

    def test_verified_observations_require_known_org_and_snapshot_hash(self):
        profiles = [{"organisation_number": "923609016", "name": "Example AS"}]
        seed = {"organisation_number": "923609016", "platform": "news", "signal_type": "public_mention", "source_url": "https://example.test/news", "content_sha256": "a" * 64, "evidence_span": "Example AS", "proof": "Exact legal name"}
        self.assertTrue(build_verified_observations([seed], profiles)[0]["exact_entity"])
        seed["content_sha256"] = "bad"
        with self.assertRaises(ValueError):
            build_verified_observations([seed], profiles)

    def test_directory_identity_is_experimental_and_never_becomes_strict(self):
        item = {
            "id": "directory-1", "organisation_number": "923609016", "platform": "company_directory",
            "signal_type": "company_profile", "source_url": "https://example.test/923609016",
            "retrieved_at": "2026-08-23T00:00:00Z", "content_sha256": "a" * 64,
            "exact_entity": True, "identity_proof": [{"type": "organisation_number", "value": "923609016"}],
            "acquisition_mode": "rights_review_experiment", "rights_status": "review_required",
            "source_class": "public_company_directory", "strategy": "company_directory_identity",
        }
        profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {}}
        score = development_score(profile, [item])
        self.assertEqual(score["components"]["exact_external_identity"], 0.0)
        self.assertEqual(score["experimental_components"]["exact_external_identity"], 20.0)
        self.assertFalse(publishable_observation(item))

    def test_fagfolk_rating_parser_uses_jsonld_and_slug_is_stable(self):
        raw = b'<script type="application/ld+json">{"aggregateRating":{"ratingValue":4.4,"ratingCount":25}}</script>'
        self.assertEqual(extract_aggregate_rating(raw)[:2], (4.4, 25))
        self.assertEqual(slug("NORDIC DØR AS"), "nordic-dor-as")


class SamplingTests(unittest.TestCase):
    def test_financial_filer_sample_requires_current_active_rows_and_preserves_overlap(self):
        fields = ["organisasjonsnummer", "navn", "organisasjonsform.kode", "sisteInnsendteAarsregnskap", "konkurs", "underAvvikling"]
        rows = [
            {"organisasjonsnummer": str(200000000 + index), "navn": f"Company {index}", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2025", "konkurs": "false", "underAvvikling": "false"}
            for index in range(12)
        ] + [
            {"organisasjonsnummer": "300000001", "navn": "Stale AS", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2024", "konkurs": "false", "underAvvikling": "false"},
            {"organisasjonsnummer": "300000002", "navn": "Bankrupt AS", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2025", "konkurs": "true", "underAvvikling": "false"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            selected, metadata = deterministic_financial_filer_sample(path, 5, latest_year="2025", preserved_organisation_numbers={"200000003"}, seed=7)
            repeated, _ = deterministic_financial_filer_sample(path, 5, latest_year="2025", preserved_organisation_numbers={"200000003"}, seed=7)
        self.assertEqual([row["organisation_number"] for row in selected], [row["organisation_number"] for row in repeated])
        self.assertIn("200000003", {row["organisation_number"] for row in selected})
        self.assertNotIn("300000001", {row["organisation_number"] for row in selected})
        self.assertNotIn("300000002", {row["organisation_number"] for row in selected})
        self.assertEqual(metadata["eligible_rows"], 12)
        self.assertEqual(metadata["preserved_eligible_selected"], 1)
        self.assertTrue(all(financial_filer_eligible(row, "2025") for row in selected))

    def test_extension_sample_is_deterministic_and_excludes_initial(self):
        fields = ["organisasjonsnummer", "navn", "hjemmeside", "organisasjonsform.kode"]
        rows = [
            {"organisasjonsnummer": str(100000000 + index), "navn": f"Company {index}", "hjemmeside": "", "organisasjonsform.kode": "AS"}
            for index in range(20)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            first, metadata = deterministic_extension_sample(path, 5, {"100000000", "100000001"}, seed=3)
            second, _ = deterministic_extension_sample(path, 5, {"100000000", "100000001"}, seed=3)
        self.assertEqual([item["organisation_number"] for item in first], [item["organisation_number"] for item in second])
        self.assertEqual(len(first), 5)
        self.assertEqual(metadata["overlap_with_excluded"], 0)

    def test_strata_distinguish_adverse_and_web_coverage(self):
        base = {"legal_form": "AS", "employees": 12, "bankrupt": False, "liquidating": False, "website": "example.no"}
        self.assertEqual(stratum(base), "AS|5-19|active|web")
        self.assertEqual(stratum({**base, "bankrupt": True, "website": ""}), "AS|5-19|adverse|no-web")

    def test_normalize_does_not_invent_employee_count(self):
        row = normalize_row({"organisasjonsnummer": "923609016", "navn": "Example AS", "antallAnsatte": ""})
        self.assertIsNone(row["employees"])
        self.assertEqual(row["latest_submitted_accounts"], "")

    def test_fresh_website_audit_sample_excludes_poc_and_deduplicates_hosts(self):
        fields = ["organisasjonsnummer", "navn", "hjemmeside", "organisasjonsform.kode"]
        rows = [
            {"organisasjonsnummer": "111111111", "navn": "Excluded AS", "hjemmeside": "excluded.no", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "222222222", "navn": "A AS", "hjemmeside": "https://www.shared.no/a", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "333333333", "navn": "B AS", "hjemmeside": "shared.no/b", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "444444444", "navn": "C AS", "hjemmeside": "unique.no", "organisasjonsform.kode": "AS"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            selected, metadata = deterministic_website_audit_sample(path, 1, {"111111111"}, {"shared.no"}, seed=9)
        self.assertEqual(len(selected), 1)
        self.assertNotIn("111111111", {row["organisation_number"] for row in selected})
        self.assertEqual(selected[0]["organisation_number"], "444444444")
        self.assertEqual(metadata["unique_hosts_selected"], 1)
        self.assertEqual(metadata["excluded_website_hosts"], 1)


class OperationsTests(unittest.TestCase):
    def test_history_rate_limiter_spaces_request_starts_not_responses(self):
        import norway_company_agent.official as official

        old = official._history_last_request
        now = [10.0]
        sleeps = []

        def clock():
            return now[0]

        def sleeper(delay):
            sleeps.append(delay)
            now[0] += delay

        try:
            official._history_last_request = 9.0
            _reserve_history_slot(clock, sleeper)
            self.assertAlmostEqual(sleeps[0], 1.1)
            self.assertAlmostEqual(official._history_last_request, 11.1)
            now[0] = 13.3
            _reserve_history_slot(clock, sleeper)
            self.assertEqual(len(sleeps), 1)
            self.assertAlmostEqual(official._history_last_request, 13.3)
        finally:
            official._history_last_request = old

    def test_batch_input_preserves_split_annotations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orgs.jsonl"
            path.write_text(json.dumps({"organisation_number": "923609016", "evaluation_split": "held_out", "sample_slice": "stress", "ignored": "x"}) + "\n", encoding="utf-8")
            self.assertEqual(read_organisation_inputs(path), [{"organisation_number": "923609016", "evaluation_split": "held_out", "sample_slice": "stress"}])

    def test_batch_contract_emits_exact_terminal_envelopes(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {
                "registry": evidence("registry", "available", "official", "https://example.test", content_sha256="a" * 64),
                "website": evidence("website", "blocked", "company_site", "https://example.test", note="robots.txt denied"),
            },
        }
        envelope = terminal_envelope(profile, run_id="day-1", modules=["registry", "website"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:01:00Z")
        self.assertEqual(envelope["modules"]["registry"]["state"], "complete")
        self.assertEqual(envelope["modules"]["website"]["state"], "blocked_robots")
        self.assertTrue(validate_envelopes([envelope], 1)["passed"])
        self.assertFalse(validate_envelopes([envelope], 2)["passed"])

    def test_unknown_evidence_state_is_submission_error(self):
        self.assertEqual(evidence_terminal_state({"status": "not_fetched"}), "submission_error")

    def test_batch_resume_only_skips_profiles_with_all_terminal_modules(self):
        complete = {"evidence": {"registry": {"status": "available"}, "website": {"status": "not_found"}}}
        partial = {"evidence": {"registry": {"status": "available"}, "website": {"status": "not_fetched"}}}
        self.assertTrue(profile_complete_for_modules(complete, ["registry", "website"]))
        self.assertFalse(profile_complete_for_modules(partial, ["registry", "website"]))

    def test_nearest_rank_percentiles_are_deterministic(self):
        self.assertEqual(percentile([1, 2, 3, 4, 100], 0.5), 3)
        self.assertEqual(percentile([1, 2, 3, 4, 100], 0.95), 100)
        self.assertEqual(latency_summary([1, 2, 3]), {"n": 3, "p50_ms": 2.0, "p95_ms": 3.0, "max_ms": 3.0})

    def test_domain_fairness_summary_preserves_tail(self):
        result = domain_request_summary(__import__("collections").Counter({"a.no": 1, "b.no": 2, "c.no": 9}))
        self.assertEqual(result["domains"], 3)
        self.assertEqual(result["p50_requests"], 2.0)
        self.assertEqual(result["max_requests"], 9)


class WebsiteTests(unittest.TestCase):
    def test_interrupted_run_does_not_synthesize_terminal_failures(self):
        profiles = [{"organisation_number": "1", "website": "pending.no"}]
        self.assertEqual(terminal_events_for_run(profiles, [], False), [])
        self.assertEqual(len(terminal_events_for_run(profiles, [], True)), 1)

    def test_missing_seed_gets_explicit_terminal_event(self):
        profiles = [
            {"organisation_number": "1", "website": "example.no"},
            {"organisation_number": "2", "website": "blocked.no"},
            {"organisation_number": "3", "website": ""},
        ]
        existing = [{"organisation_number": "1", "status": "available"}]
        missing = missing_seed_error_events(profiles, existing)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["organisation_number"], "2")
        self.assertEqual(missing[0]["status"], "source_error")
        self.assertIn("robots.txt", missing[0]["error"])

    def test_crawl_event_extraction_and_merge_preserve_page_hashes(self):
        homepage = extract_page_event(
            organisation_number="923609016",
            requested_url="https://example.no/",
            final_url="https://example.no/",
            status_code=200,
            content_type="text/html; charset=utf-8",
            body=b'<html><head><title>Example AS</title><meta name="description" content="Company"></head><body><p>Example AS provides enough substantive company information for extraction and identity review.</p><a href="https://linkedin.com/company/example">LinkedIn</a></body></html>',
            page_kind="homepage",
            retrieved_at="2026-08-22T00:00:00Z",
        )
        secondary = extract_page_event(
            organisation_number="923609016",
            requested_url="https://example.no/contact",
            final_url="https://example.no/contact",
            status_code=200,
            content_type="text/html",
            body=b"<html><title>Contact</title><body>Contact Example AS in Oslo.</body></html>",
            page_kind="priority",
            retrieved_at="2026-08-22T00:00:01Z",
        )
        record = merge_profile_events({"website": "https://example.no"}, [homepage, secondary])
        self.assertEqual(record["status"], "available")
        self.assertEqual(len(record["value"]["pages"]), 2)
        self.assertEqual(record["content_sha256"], homepage["content_sha256"])
        self.assertEqual(record["value"]["scheduler"], "scrapy_resumable_v1")

    def test_footer_identity_is_preserved_for_exact_company_gate(self):
        event = extract_page_event(
            organisation_number="985628572",
            requested_url="https://netsolution.no/",
            final_url="https://netsolution.no/",
            status_code=200,
            content_type="text/html",
            body=(
                b'<html><head><title>IT services</title></head><body><main>Useful services for customers.</main>'
                b'<footer>Netsolution Viken AS, Kobbervikdalen 75 A, 3036 Drammen</footer></body></html>'
            ),
            page_kind="homepage",
            retrieved_at="2026-08-23T00:00:00Z",
        )
        website = merge_profile_events({"website": "https://netsolution.no/"}, [event])
        profile = {
            "organisation_number": "985628572",
            "name": "NETSOLUTION VIKEN AS",
            "evidence": {"website": website},
        }
        self.assertIn("Netsolution Viken AS", website["value"]["identity_text_excerpt"])
        self.assertTrue(assess_website_identity(profile)["publishable"])

    def test_normalizes_registry_hostname(self):
        self.assertEqual(normalize_homepage("example.no"), "https://example.no/")
        self.assertEqual(normalize_homepage("http://example.no"), "http://example.no/")

    def test_only_extracts_declared_social_links(self):
        soup = BeautifulSoup('<a href="https://www.linkedin.com/company/example/">LinkedIn</a><a href="/about">About</a>', "html.parser")
        self.assertEqual(_social_links("https://example.no", soup), [{"platform": "linkedin", "url": "https://linkedin.com/company/example"}])

    def test_extracts_embedded_company_social_profiles(self):
        soup = BeautifulSoup(
            '<div class="fb-page" data-href="https://www.facebook.com/ExampleCompany"></div>'
            '<iframe src="https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2FSecondCompany"></iframe>',
            "html.parser",
        )
        self.assertEqual(
            _social_links("https://example.no/", soup),
            [
                {"platform": "facebook", "url": "https://facebook.com/ExampleCompany"},
                {"platform": "facebook", "url": "https://facebook.com/SecondCompany"},
            ],
        )

    def test_extracts_schema_same_as_company_social_profiles(self):
        value = [{
            "@type": "Organization",
            "sameAs": [
                "https://www.facebook.com/ExampleCompany/",
                "https://instagram.com/examplecompany",
                "https://linkedin.com/in/example-person",
            ],
        }]
        self.assertEqual(
            structured_social_links(value),
            [
                {"platform": "facebook", "url": "https://facebook.com/ExampleCompany"},
                {"platform": "instagram", "url": "https://instagram.com/examplecompany"},
            ],
        )

    def test_social_profiles_reject_share_event_group_and_policy_links(self):
        rejected = (
            "https://facebook.com/sharer.php?u=x", "https://facebook.com/events/123",
            "https://facebook.com/groups/123", "https://facebook.com/policy.php",
            "https://facebook.com/privacy/explanation",
            "https://linkedin.com/shareArticle?url=x", "https://instagram.com/p/abc",
        )
        self.assertTrue(all(normalize_social_url(url) is None for url in rejected))

    def test_social_profiles_canonicalize_www_variants(self):
        self.assertEqual(normalize_social_url("https://www.facebook.com/Example/"), {"platform": "facebook", "url": "https://facebook.com/Example"})
        self.assertEqual(normalize_social_url("https://linkedin.com/company/example/admin/feed/posts"), {"platform": "linkedin", "url": "https://linkedin.com/company/example"})
        self.assertEqual(normalize_social_url("https://youtube.com/channel/abc/featured"), {"platform": "youtube", "url": "https://youtube.com/channel/abc"})
        self.assertIsNone(normalize_social_url("https://facebook.com/profile.php"))
        self.assertIsNone(normalize_social_url("https://[object Object]"))

    def test_priority_pages_stay_on_exact_site(self):
        soup = BeautifulSoup('<a href="/kontakt">Contact</a><a href="https://other.no/about">About</a><a href="/products">Products</a>', "html.parser")
        self.assertEqual(_priority_links("https://example.no/", soup), ["https://example.no/kontakt"])

    def test_js_shell_is_only_a_fallback_candidate(self):
        shell = BeautifulSoup('<html><script src="a.js"></script><script src="b.js"></script></html>', "html.parser")
        self.assertEqual(_extraction_state("", shell), "js_fallback_candidate")
        self.assertEqual(_extraction_state("A" * 100, shell), "static_complete")

    def test_blocks_local_network_targets(self):
        for url in ("http://127.0.0.1/admin", "http://localhost/", "http://169.254.169.254/latest/meta-data"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                assert_public_url(url)


class DiscoveryTests(unittest.TestCase):
    def test_brave_request_keeps_key_out_of_url_and_parses_in_memory(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"web": {"results": [{
                    "url": "https://example.no", "title": "Example AS", "description": "Example in Oslo",
                }]}}).encode()

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["key"] = request.get_header("X-subscription-token")
            captured["timeout"] = timeout
            return Response()

        with patch("scripts.run_brave_discovery.urllib.request.urlopen", fake_open):
            results, operation = brave_search({
                "name": "Example AS", "organisation_number": "999999999", "municipality": "OSLO",
            }, "secret-test-key", timeout=3.0, count=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(operation["status"], 200)
        self.assertNotIn("secret-test-key", captured["url"])
        self.assertEqual(captured["key"], "secret-test-key")
        self.assertEqual(captured["timeout"], 3.0)

    def test_company_query_contains_exact_name_org_and_location(self):
        query = build_company_search_query({
            "name": "Norsk Fiskeeksport AS", "organisation_number": "923 609 016", "municipality": "NOTODDEN",
        })
        self.assertEqual(query, '"Norsk Fiskeeksport AS" 923609016 NOTODDEN')

    def test_brave_parser_is_provider_neutral_candidate_input(self):
        results = parse_brave_web_results({"web": {"results": [
            {"url": "https://example.no", "title": "Example AS", "description": "Example in Oslo"},
            {"title": "Missing URL"},
        ]}}, query='"Example AS" 999999999')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["provider"], "brave_search_api")
        self.assertEqual(results[0]["rank"], 1)

    def test_directory_and_social_results_are_not_company_site_candidates(self):
        profile = {"organisation_number": "923609016", "name": "Example Norge AS", "municipality": "OSLO"}
        for url in ("https://proff.no/selskap/example", "https://linkedin.com/company/example"):
            with self.subTest(url=url):
                self.assertEqual(score_search_candidate(profile, {"url": url, "title": "Example Norge AS"})["status"], "rejected")

    def test_exact_name_in_title_and_host_is_only_a_crawl_candidate(self):
        profile = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "municipality": "NOTODDEN"}
        decision = choose_search_candidate(profile, [{
            "url": "https://norskfiskeeksport.no/",
            "title": "Norsk Fiskeeksport AS",
            "snippet": "Seafood exporter in Notodden",
            "rank": 1,
            "provider": "fixture",
        }])
        self.assertFalse(decision["abstained"])
        self.assertEqual(decision["selected"]["status"], "accepted_for_crawl")
        self.assertIn("Publication still requires", decision["policy"])

    def test_ambiguous_name_match_without_host_support_abstains(self):
        profile = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "municipality": "NOTODDEN"}
        decision = choose_search_candidate(profile, [{"url": "https://parent-group.no/", "title": "Norsk Fiskeeksport AS - portfolio", "snippet": "Group companies"}])
        self.assertTrue(decision["abstained"])


class SentimentTests(unittest.TestCase):
    @staticmethod
    def item(item_id, label="positive", source="https://news.example/a", **changes):
        item = {
            "id": str(item_id), "label": label, "exact_entity": True,
            "source_class": "licensed_news", "source_url": source,
            "retrieved_at": "2026-08-22T00:00:00Z", "evidence_span": "Exact-company event sentence.",
            "content_sha256": "a" * 64,
        }
        item.update(changes)
        return item

    def test_company_owned_or_unhashed_items_are_not_publishable(self):
        self.assertFalse(publishable_sentiment_item(self.item(1, source_class="company_owned")))
        self.assertFalse(publishable_sentiment_item(self.item(1, content_sha256=None)))

    def test_inference_input_requires_exact_entity_independent_source_and_hash(self):
        item = self.item(1, text="Selskapet vant en ny kontrakt.")
        self.assertTrue(sentiment_input_eligibility(item)[0])
        accepted, reasons = sentiment_input_eligibility({**item, "exact_entity": False, "content_sha256": None})
        self.assertFalse(accepted)
        self.assertIn("exact company identity is not verified", reasons)
        self.assertIn("missing content_sha256", reasons)

    def test_pinned_model_output_normalization_is_closed_set(self):
        self.assertEqual(len(MODEL_REVISION), 40)
        self.assertEqual(normalize_generated_label(" Positive. "), "positive")
        self.assertIsNone(normalize_generated_label("bullish"))

    def test_rollup_requires_two_independent_publishers(self):
        same_publisher = [
            self.item(1, source="https://news.example/a"),
            self.item(2, source="https://news.example/b"),
        ]
        self.assertEqual(aggregate_company_sentiment(same_publisher)["status"], "abstain")
        independent = same_publisher + [self.item(3, source="https://other.example/c")]
        result = aggregate_company_sentiment(independent)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["label"], "positive")

    def test_perfect_balanced_300_item_corpus_passes_poc_gate(self):
        labels = ("positive", "neutral", "negative", "mixed")
        gold = [{"id": str(i), "label": labels[i % 4]} for i in range(300)]
        predictions = [self.item(i, labels[i % 4], source=f"https://news{i % 7}.example/item/{i}") for i in range(300)]
        report = evaluate_predictions(gold, predictions)
        self.assertEqual(report["accuracy"], 1.0)
        self.assertEqual(report["macro_f1"], 1.0)
        self.assertTrue(report["qualification_passed"])
        self.assertFalse(report["production_scale_gate_passed"])

    def test_wrong_entity_and_company_owned_predictions_fail_gate(self):
        labels = ("positive", "neutral", "negative")
        gold = [{"id": str(i), "label": labels[i % 3]} for i in range(300)]
        predictions = [self.item(i, labels[i % 3], source=f"https://news.example/{i}") for i in range(300)]
        predictions[0]["exact_entity"] = False
        predictions[1]["source_class"] = "company_owned"
        report = evaluate_predictions(gold, predictions)
        self.assertEqual(report["wrong_entity_predictions"], 1)
        self.assertEqual(report["company_owned_predictions"], 1)
        self.assertFalse(report["qualification_passed"])

    def test_qualification_minimum_cannot_be_weakened(self):
        with self.assertRaises(ValueError):
            evaluate_predictions([{"id": "1", "label": "positive"}], [self.item(1)], minimum_items=1)


class OfficialNormalizationTests(unittest.TestCase):
    def test_accounting_obligation_is_categorical_for_as_but_not_enk(self):
        company = accounting_obligation_assessment({"organisation_number": "923609016", "legal_form": "AS"})
        sole_trader = accounting_obligation_assessment({"organisation_number": "923609017", "legal_form": "ENK", "employees": 0})
        self.assertEqual(company["value"]["classification"], "required_by_legal_form")
        self.assertEqual(sole_trader["value"]["classification"], "threshold_or_activity_dependent")
        self.assertTrue(company["content_sha256"])
        self.assertEqual(company["source_row_key"], "923609016")

    def test_observed_filing_overrides_rule_path(self):
        record = accounting_obligation_assessment({"organisation_number": "923609018", "legal_form": "ENK", "latest_submitted_accounts": "2024"})
        self.assertEqual(record["value"]["classification"], "filing_observed")

    def test_entity_normalization_keeps_identity(self):
        record = normalize_entity({"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA", "organisasjonsform": {"kode": "ASA"}})
        self.assertEqual(record["organisation_number"], "923609016")
        self.assertEqual(record["legal_form"], "ASA")

    def test_financial_fields_keep_period_currency_and_zero(self):
        body = [{"id": 1, "valuta": "NOK", "regnskapsperiode": {"tilDato": "2025-12-31"}, "resultatregnskapResultat": {"driftsresultat": {"driftsresultat": 0, "driftsinntekter": {"sumDriftsinntekter": 12}}}}]
        record = normalize_financials(body)["records"][0]
        self.assertEqual(record["revenue"], 12)
        self.assertEqual(record["operating_result"], 0)
        self.assertEqual(record["currency"], "NOK")

    def test_financial_history_is_sorted_and_links_to_official_pdfs(self):
        record = normalize_financial_history(["2024", "2022", "2024", "invalid"], "923609016")
        self.assertEqual(record["years"], ["2022", "2024"])
        self.assertEqual(record["pdfs"][0]["year"], "2024")
        self.assertTrue(record["pdfs"][0]["url"].endswith("/923609016/2024"))

    def test_public_roles_drop_birth_dates(self):
        body = {"rollegrupper": [{"type": {"kode": "STYR"}, "roller": [{"type": {"kode": "LEDE", "beskrivelse": "Chair"}, "person": {"fodselsdato": "1970-01-01", "navn": {"fornavn": "Ada", "etternavn": "Nord"}}}]}]}
        record = normalize_roles(body)["roles"][0]
        self.assertEqual(record["name"], "Ada Nord")
        self.assertNotIn("fodselsdato", json.dumps(record))


class ResearchAgentTests(unittest.TestCase):
    @staticmethod
    def screen_row(org, municipality, employees, revenue):
        return {
            "organisation_number": org,
            "name": f"Company {org}",
            "municipality": municipality,
            "employees": employees,
            "evidence": {
                "registry": evidence("registry", "available", "official_registry_bulk", "https://example.test/registry", content_sha256="a" * 64),
                "financials": evidence("financials", "available", "official_annual_accounts", "https://example.test/accounts", value={"records": [{"revenue": revenue, "annual_result": 1}]}, content_sha256="b" * 64),
            },
        }

    def test_cross_company_screen_has_exact_membership_and_inspectable_plan(self):
        rows = [
            self.screen_row("111111111", "OSLO", 20, 2_000_000),
            self.screen_row("222222222", "OSLO", 5, 2_000_000),
            self.screen_row("333333333", "BERGEN", 20, 2_000_000),
            self.screen_row("444444444", "OSLO", 20, 500_000),
        ]
        result = screen_profiles(rows, "companies in Oslo with more than 10 employees and revenue over 1 million")
        self.assertFalse(result["abstained"])
        self.assertEqual([item["organisation_number"] for item in result["results"]], ["111111111"])
        self.assertEqual(len(result["plan"]["filters"]), 3)
        self.assertTrue(all(citation["content_sha256"] for citation in result["results"][0]["citations"]))

    def test_unsupported_cross_company_criterion_abstains(self):
        plan = parse_screen_query("companies in Oslo with positive Glassdoor sentiment")
        self.assertFalse(plan["executable"])
        result = screen_profiles([], "companies in Oslo with positive Glassdoor sentiment")
        self.assertTrue(result["abstained"])
        self.assertIn("Glassdoor", result["reason"])

    def test_missing_website_is_not_treated_as_proven_absence(self):
        result = screen_profiles([], "companies without a website")
        self.assertTrue(result["abstained"])
        self.assertIn("does not prove", result["reason"])

    def test_workspace_saves_pins_history_and_recovers_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            workspace, warning = load_workspace(path)
            self.assertIsNone(warning)
            updated = record_screen(workspace, {"query": "in Oslo", "plan": {"filters": []}, "result_count": 1, "results": [{"organisation_number": "111111111"}]}, pin_organisations=["111111111"])
            save_workspace(path, updated)
            loaded, warning = load_workspace(path)
            self.assertEqual(loaded["pins"], ["111111111"])
            self.assertEqual(len(loaded["history"]), 1)
            path.write_text("not json", encoding="utf-8")
            recovered, warning = load_workspace(path)
            self.assertEqual(recovered["pins"], [])
            self.assertIn("recovery", warning or "")

    def test_sentiment_abstains_and_every_fact_has_a_source(self):
        row = {
            "organisation_number": "923609016", "name": "Example AS", "legal_form": "AS",
            "municipality": "OSLO", "employees": None,
            "evidence": {"registry": evidence("registry", "available", "official", "https://example.test")},
        }
        result = answer_profile(row, "Give me employee sentiment")
        self.assertTrue(any("Sentiment is not scored" in item for item in result["unsupported_or_uncertain"]))
        self.assertTrue(all(fact["source_url"] for fact in result["facts"]))
        self.assertFalse(any(fact["claim"] == "Registry employee count" for fact in result["facts"]))

    def test_natural_leads_word_routes_to_roles(self):
        roles = evidence("roles", "available", "official", "https://example.test/roles", value={"roles": [{"name": "Ada Nord", "role": "Chair", "inactive": False}]})
        row = {"organisation_number": "923609016", "name": "Example AS", "evidence": {"roles": roles}}
        result = answer_profile(row, "Who leads this company?")
        self.assertEqual(result["facts"][0]["value"], "Ada Nord")

    def test_quarantined_website_claims_are_not_returned(self):
        website = evidence("website", "available", "company_site", "https://parent.test", value={"description": "Parent claim", "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/parent"}], "identity_assessment": {"publishable": False}})
        row = {"organisation_number": "923609016", "name": "Subsidiary AS", "evidence": {"website": website}}
        result = answer_profile(row, "What social information is available?")
        self.assertFalse(result["facts"])
        self.assertTrue(any("quarantined" in item for item in result["unsupported_or_uncertain"]))


class PrototypeTests(unittest.TestCase):
    def test_qualification_copy_keeps_production_boundary_visible(self):
        header, boundary = qualification_copy({
            "weighted_score": {"verified_points": 100, "maximum_points": 100},
            "qualification": {"poc_qualified": True, "production_qualified": False},
        })
        self.assertIn("100/100", header)
        self.assertIn("not production-qualified", header)
        self.assertIn("remain quarantined", boundary)

    def test_quarantined_social_links_are_counted_but_not_published(self):
        row = {
            "organisation_number": "923609016",
            "name": "Subsidiary AS",
            "legal_form": "AS",
            "employees": None,
            "municipality": "OSLO",
            "industry_code": None,
            "industry_label": None,
            "website": "https://parent.test",
            "bankrupt": False,
            "liquidating": False,
            "evidence": {
                "website": evidence(
                    "website",
                    "available",
                    "company_site",
                    "https://parent.test",
                    value={
                        "identity_assessment": {"publishable": False},
                        "social_links": [],
                        "discovered_social_links": [
                            {"platform": "linkedin", "url": "https://linkedin.com/company/parent"}
                        ],
                    },
                )
            },
        }
        website = compact_prototype(row)["web"]["value"]
        self.assertEqual(website["quarantined_social_count"], 1)
        self.assertEqual(website["social_links"], [])


class RefreshTests(unittest.TestCase):
    def test_snapshot_fetcher_hashes_evaluator_bytes_and_carries_times(self):
        url = "https://example.test/entity/1"
        fetcher = SnapshotFetcher({"retrieved_at": "2026-01-02T00:00:00Z", "effective_at": "2026-01-01T00:00:00Z", "responses": {url: {"body": {"value": 1}}}})
        result = fetcher(url)
        self.assertEqual(result.status, 200)
        self.assertEqual(len(result.content_sha256 or ""), 64)
        self.assertEqual(result.effective_at, "2026-01-01T00:00:00Z")

    def test_identical_refresh_is_an_idempotent_noop(self):
        row = {"organisation_number": "923609016", "name": "Example AS", "employees": 4}
        self.assertEqual(diff_profile(row, dict(row)), [])

    def test_missing_to_zero_is_a_real_change_with_provenance(self):
        source = evidence("registry", "available", "official", "https://example.test/entity")
        old = {"organisation_number": "923609016", "employees": None, "evidence": {"registry": source}}
        new = {"organisation_number": "923609016", "employees": 0, "evidence": {"registry": source}}
        change = diff_profile(old, new)[0]
        self.assertIsNone(change["old_value"])
        self.assertEqual(change["new_value"], 0)
        self.assertEqual(change["source_url"], "https://example.test/entity")

    def test_refresh_rejects_membership_or_identity_drift(self):
        with self.assertRaises(ValueError):
            diff_datasets([{"organisation_number": "923609016"}], [{"organisation_number": "999999999"}])


class WebsiteIdentityTests(unittest.TestCase):
    def test_group_contact_page_listing_subsidiary_org_number_is_not_exact_homepage_identity(self):
        profile = {
            "organisation_number": "915637353",
            "name": "SKS PRODUKSJON AS",
            "evidence": {"website": evidence("website", "available", "company_site", "https://sks.no", value={
                "final_url": "https://sks.no/",
                "title": "Konsern - SKS - Forside",
                "main_text_excerpt": "SKS is a power group with multiple subsidiaries.",
                "pages": [{"title": "Contact", "main_text_excerpt": "SKS Produksjon AS organisation number 915 637 353"}],
            })},
        }
        assessment = assess_website_identity(profile)
        self.assertFalse(assessment["publishable"])
        self.assertNotEqual(assessment["score"], 1.0)

    def test_shared_identity_gate_quarantines_parent_social_links(self):
        website = evidence("website", "available", "company_site", "https://parent.test", value={
            "title": "Parent Group", "main_text_excerpt": "Parent Group portfolio",
            "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/parent"}],
        })
        profile = {"organisation_number": "923609016", "name": "Exact Subsidiary AS", "evidence": {}}
        result = apply_website_identity_gate(profile, website)
        self.assertFalse(result["assessment"]["publishable"])
        self.assertEqual(result["website"]["value"]["social_links"], [])
        self.assertEqual(result["quarantined_social_links"], 1)

    def test_exact_legal_name_is_publishable(self):
        row = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "evidence": {"website": {"status": "available", "value": {"title": "Norsk Fiskeeksport AS"}}}}
        self.assertTrue(assess_website_identity(row)["publishable"])

    def test_parent_brand_without_legal_name_is_quarantined(self):
        row = {"organisation_number": "988412406", "name": "Tevlingveien 23 Invest AS", "evidence": {"website": {"status": "available", "value": {"title": "Ragde Eiendom"}}}}
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_parked_domain_and_parent_company_sports_site_are_quarantined(self):
        parked = {"organisation_number": "996081001", "name": "Condalign AS", "evidence": {"website": {"status": "available", "value": {"title": "CondAlign.com is for sale | HugeDomains"}}}}
        sports = {"organisation_number": "996242692", "name": "Primulator B.I.L.", "evidence": {"website": {"status": "available", "value": {"title": "Primulator", "description": "Premium products for HoReCa"}}}}
        self.assertFalse(assess_website_identity(parked)["publishable"])
        self.assertFalse(assess_website_identity(sports)["publishable"])

    def test_hosting_placeholder_and_generic_link_page_are_quarantined(self):
        hosting = {"organisation_number": "917568278", "name": "HJELMEN AS", "evidence": {"website": {"status": "available", "value": {"title": "www.Hjelmen-as.no is parked at Miss Hosting Web Hosting", "main_text_excerpt": "Hjelmen " * 100}}}}
        links = {"organisation_number": "986606009", "name": "KOALA ANS", "evidence": {"website": {"status": "available", "value": {"title": "koala.no", "description": "Find the best information and most relevant links on all topics related to"}}}}
        self.assertFalse(assess_website_identity(hosting)["publishable"])
        self.assertFalse(assess_website_identity(links)["publishable"])

    def test_broader_umbrella_site_is_not_exact_when_name_only_appears_in_body(self):
        row = {
            "organisation_number": "976994027",
            "name": "AVALDSNES SOKN",
            "evidence": {"website": {"status": "available", "value": {
                "title": "Kirken i Karmøy",
                "final_url": "https://www.karmoykirken.no/",
                "main_text_excerpt": "Avaldsnes sokn is one of several parishes represented on this umbrella site.",
            }}},
        }
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_social_handle_requires_exact_entity_name_evidence(self):
        aon = {"name": "Aon Norway AS"}
        fish = {"name": "Norsk Fiskeeksport AS"}
        self.assertFalse(assess_social_identity(aon, {"platform": "linkedin", "url": "https://linkedin.com/company/aon"})["publishable"])
        self.assertTrue(assess_social_identity(fish, {"platform": "linkedin", "url": "https://linkedin.com/company/norsk-fiskeeksport"})["publishable"])


class VerifiedSiteSeedTests(unittest.TestCase):
    def test_verified_seed_is_applied_and_unknown_org_is_rejected(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles.jsonl"
            seeds = root / "seeds.json"
            output = root / "output.jsonl"
            report = root / "report.json"
            profiles.write_text(json.dumps({"organisation_number": "123456789", "name": "Example AS"}) + "\n")
            seeds.write_text(json.dumps([{
                "organisation_number": "123456789",
                "website": "https://example.no/",
                "proof_url": "https://source.example/proof",
                "proof": "Exact name and organisation number",
            }]))
            command = [
                sys.executable, str(ROOT / "scripts" / "apply_verified_site_seeds.py"),
                "--profiles", str(profiles), "--seeds", str(seeds),
                "--output", str(output), "--report", str(report),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            row = json.loads(output.read_text().strip())
            self.assertEqual(row["website"], "https://example.no/")
            self.assertEqual(row["website_seed_source"], "independently_verified_exact_entity")
            self.assertEqual(json.loads(report.read_text())["applied"], 1)

            seeds.write_text(json.dumps([{
                "organisation_number": "987654321",
                "website": "https://unknown.no/",
                "proof_url": "https://source.example/proof",
            }]))
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("unknown organisations", failed.stderr)


class ObservationGateTests(unittest.TestCase):
    """Entity, hash, duplicate, freshness and rights hardening for external observations."""

    def observation(self, **changes):
        base = {
            "id": "obs-1",
            "organisation_number": "923609016",
            "platform": "google_places",
            "signal_type": "place_summary",
            "source_url": "https://maps.example.invalid/place/example",
            "retrieved_at": "2026-08-20T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "synthetic_identity_proof"}],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "public_business_listing",
        }
        return {**base, **changes}

    def test_gate_binds_observation_to_the_profile_organisation(self):
        self.assertTrue(publishable_observation(self.observation()))
        self.assertTrue(publishable_observation(self.observation(), organisation_number="923609016"))
        self.assertFalse(publishable_observation(self.observation(), organisation_number="987654321"))
        self.assertIn(
            "observation organisation number does not match the profile",
            validate_observation(self.observation(), organisation_number="987654321"),
        )

    def test_hash_check_is_syntax_not_truth(self):
        self.assertTrue(publishable_observation(self.observation(content_sha256="a" * 64)))
        for bad in ("z" * 64, "A" * 64, "a" * 63, "a" * 65, "sha256:" + "a" * 64):
            self.assertIn("invalid content hash", validate_observation(self.observation(content_sha256=bad)))
        self.assertIn("missing content hash", validate_observation(self.observation(content_sha256=None)))

    def test_unmatched_observation_cannot_contaminate_a_profile(self):
        outsider = self.observation(**{"id": "outsider", "organisation_number": "999999999"})
        gate = gate_observations([outsider], organisation_numbers=["923609016", "987654321"])
        self.assertEqual(gate["accepted"], [])
        self.assertEqual(len(gate["unmatched"]), 1)
        self.assertEqual(gate["unmatched"][0]["organisation_number"], "999999999")
        self.assertEqual(gate["by_organisation"]["923609016"]["accepted_observations"], 0)
        self.assertEqual(gate["by_organisation"]["987654321"]["accepted_observations"], 0)
        self.assertIsNone(company_external_block(gate, "923609016"))
        self.assertIsNone(company_external_block(gate, "999999999"))

    def test_identical_duplicates_collapse_and_conflicting_duplicates_are_refused(self):
        identical = self.observation(signal_type="review", evidence_span="Same evidence")
        unique, report = deduplicate_observations([identical, dict(identical), dict(identical)])
        self.assertEqual(len(unique), 1)
        self.assertEqual(report["collapsed_identical"], 2)
        self.assertEqual(report["conflicting_groups"], [])

        conflict_a = self.observation(**{"id": "dup", "source_url": "https://maps.example.invalid/place/a"})
        conflict_b = self.observation(**{"id": "dup", "source_url": "https://maps.example.invalid/place/b"})
        gate = gate_observations([conflict_a, conflict_b], organisation_numbers=["923609016"])
        self.assertEqual(gate["accepted"], [])
        self.assertEqual(gate["duplicates"]["conflicting_groups"][0]["distinct_payloads"], 2)
        self.assertEqual(len(gate["rejected"]), 2)
        for entry in gate["rejected"]:
            self.assertEqual(entry["reasons"], ["conflicting duplicate observations for the same organisation and id"])

    def test_same_observation_id_across_organisations_is_not_treated_as_a_collision(self):
        first = self.observation(**{"id": "shared", "organisation_number": "923609016", "source_url": "https://maps.example.invalid/place/a"})
        second = self.observation(**{"id": "shared", "organisation_number": "987654321", "source_url": "https://maps.example.invalid/place/b"})
        gate = gate_observations([first, second], organisation_numbers=["923609016", "987654321"])
        self.assertEqual(len(gate["accepted"]), 2)
        self.assertEqual(gate["duplicates"]["ids_shared_across_organisations"], ["shared"])
        self.assertEqual(gate["duplicates"]["conflicting_groups"], [])
        self.assertEqual(gate["rejected"], [])
        self.assertEqual([item["id"] for item in gate["accepted"] if item["organisation_number"] == "923609016"], ["shared"])
        self.assertEqual([item["id"] for item in gate["accepted"] if item["organisation_number"] == "987654321"], ["shared"])

    def test_content_duplicates_with_distinct_ids_are_reported_but_kept(self):
        first = self.observation(**{"id": "a", "source_url": "https://maps.example.invalid/place/a"})
        second = self.observation(**{"id": "b", "source_url": "https://maps.example.invalid/place/a"})
        gate = gate_observations([first, second], organisation_numbers=["923609016"])
        self.assertEqual(gate["duplicates"]["content_duplicate_ids"], ["a", "b"])
        self.assertEqual(len(gate["accepted"]), 2)

    def test_rights_failure_is_reported_and_never_reaches_the_published_block(self):
        blocked = self.observation(rights_status="review_required")
        allowed = self.observation(**{"id": "obs-2", "source_url": "https://maps.example.invalid/place/allowed"})
        gate = gate_observations([blocked, allowed], organisation_numbers=["923609016"])
        self.assertEqual([item["id"] for item in gate["accepted"]], ["obs-2"])
        self.assertEqual(gate["rejected"][0]["reasons"], ["source rights are not approved"])
        block = company_external_block(gate, "923609016")
        self.assertEqual(block["accepted_observation_ids"], ["obs-2"])
        self.assertEqual(block["rejected_observations"], [{"id": "obs-1", "reasons": ["source rights are not approved"]}])
        self.assertEqual(block["footprint"]["accepted_observations"], 1)

    def test_acceptance_and_freshness_are_separate_signals(self):
        stale = self.observation(**{"id": "stale", "signal_type": "job_posting", "retrieved_at": "2020-01-01T00:00:00Z", "evidence_span": "old"})
        undated = self.observation(**{"id": "undated", "retrieved_at": "not-a-date", "source_url": "https://maps.example.invalid/place/undated"})
        gate = gate_observations([stale, undated], organisation_numbers=["923609016"], as_of="2026-08-24T00:00:00Z")
        footprint = gate["footprint"]
        self.assertEqual(footprint["accepted_observations"], 2)   # acceptance is unchanged by age
        self.assertEqual(footprint["fresh_observations"], 0)
        self.assertEqual(footprint["stale_observations"], 2)
        self.assertEqual(footprint["undated_observations"], 1)
        self.assertEqual(footprint["counts_scope"], "accepted")
        self.assertEqual(footprint["counted_observations"], 2)
        self.assertFalse(footprint["freshness_policy"]["undated_is_fresh"])

        enforced = gate_observations(
            [stale, undated],
            organisation_numbers=["923609016"],
            as_of="2026-08-24T00:00:00Z",
            enforce_freshness=True,
        )["footprint"]
        self.assertEqual(enforced["accepted_observations"], 2)
        self.assertEqual(enforced["counted_observations"], 0)
        self.assertEqual(enforced["active_job_count"], 0)
        self.assertEqual(enforced["stale_observations"], 2)
        self.assertEqual(enforced["counts_scope"], "fresh")

    def test_reviewer_gate_counts_one_opinion_per_reviewer_and_host(self):
        items = [
            self.observation(
                id=f"r-{index}",
                organisation_number="923609016",
                platform="google_places",
                signal_type="review",
                source_url="https://maps.example.invalid/place/example",
                evidence_span="Great",
                reviewer_id="same-reviewer",
                source_class="customer_review",
                sentiment_label="positive",
                sentiment_model_version="fixture-v1",
            )
            for index in range(10)
        ]
        footprint = aggregate_footprint(items, as_of="2026-08-24T00:00:00Z")
        self.assertEqual(footprint["sentiment"]["independent_reviewers"], 1)
        self.assertEqual(footprint["sentiment"]["status"], "abstain")

    def test_gate_output_is_deterministic_and_aggregation_matches_the_records(self):
        records = [
            self.observation(),
            self.observation(**{"id": "obs-2", "source_url": "https://maps.example.invalid/place/2"}),
            self.observation(**{"id": "obs-3", "organisation_number": "987654321", "source_url": "https://maps.example.invalid/place/3"}),
            self.observation(**{"id": "obs-4", "rights_status": "unknown"}),
        ]
        first = gate_observations(records, organisation_numbers=["923609016", "987654321"], as_of="2026-08-24T00:00:00Z")
        second = gate_observations(records, organisation_numbers=["923609016", "987654321"], as_of="2026-08-24T00:00:00Z")
        self.assertEqual(json.dumps(first, sort_keys=True, default=str), json.dumps(second, sort_keys=True, default=str))
        self.assertEqual(first["footprint"]["accepted_observations"], 3)
        self.assertEqual(first["footprint"]["platform_counts"], {"google_places": 3})
        self.assertEqual(
            sum(item["accepted_observations"] for item in first["by_organisation"].values()),
            first["footprint"]["accepted_observations"],
        )
        self.assertEqual(
            len(first["accepted"]) + len(first["rejected"]) + len(first["unmatched"]),
            first["duplicates"]["unique_records"],
        )
        summary = run_external_summary(first, organisation_numbers=["923609016", "987654321"])
        self.assertEqual(summary["accepted"], first["footprint"]["accepted_observations"])
        self.assertEqual(summary["rejection_reasons"]["source rights are not approved"], 1)

    def test_malformed_jsonl_is_reported_and_never_dropped_silently(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "observations.jsonl"
            path.write_text(
                json.dumps(self.observation()) + "\n"
                + "{not json}\n"
                + '["not", "an", "object"]\n'
                + "\n"
                + json.dumps(self.observation(**{"id": "obs-2", "source_url": "https://maps.example.invalid/place/2"})) + "\n",
                encoding="utf-8",
            )
            records, malformed = read_observation_file(path)
            self.assertEqual(len(records), 2)
            self.assertEqual([item["line"] for item in malformed], [2, 3])
            self.assertIn("invalid JSON", malformed[0]["error"])
            self.assertIn("expected object", malformed[1]["error"])
            gate = gate_observations(records, organisation_numbers=["923609016"], malformed=malformed)
            self.assertEqual(gate["duplicates"]["input_records"], 2)
            self.assertEqual(len(gate["malformed"]), 2)
            summary = run_external_summary(gate, organisation_numbers=["923609016"])
            self.assertEqual(summary["malformed"], 2)

    def test_missing_observation_file_is_an_operator_error(self):
        with self.assertRaises(ObservationInputError):
            read_observation_file(Path("/nonexistent/observations.jsonl"))

    def test_fixture_coverage_and_connector_policy_are_measurable(self):
        valid_path = ROOT / "tests" / "fixtures" / "external-observations-valid.jsonl"
        records, malformed = read_observation_file(valid_path)
        self.assertEqual(malformed, [])
        gate = gate_observations(
            records,
            organisation_numbers=["923609016", "987654321", "123456789"],
            as_of="2026-08-24T00:00:00Z",
        )
        summary = run_external_summary(gate, organisation_numbers=["923609016", "987654321", "123456789"])
        self.assertEqual(summary["accepted"], 3)
        self.assertEqual(summary["organisations_with_publishable_observation"], 2)
        self.assertEqual(summary["fresh_coverage"], round(2 / 3, 6))
        self.assertTrue(summary["connector_policy_passed"])
        self.assertEqual(summary["hash_verification"]["recomputed_from_bytes"], False)


class OutputContractTests(unittest.TestCase):
    """The documented envelope contract is emitted and enforced at the serialization boundary."""

    def profile(self, **changes):
        profile = {
            "organisation_number": "923609016",
            "name": "AF GRUPPEN ASA",
            "evidence": {
                "registry": evidence("registry", "available", "official_registry_bulk", "https://data.brreg.no/x", value={"navn": "AF GRUPPEN ASA"}),
                "financials": evidence("financials", "not_found", "official_registry_api", "https://data.brreg.no/y", note="No annual accounts"),
                "website": evidence("website", "blocked", "registry_linked_company_website", "https://afgruppen.no/", note="robots.txt disallowed"),
            },
            "run_metrics": {"requests": 2, "runtime_ms": 12},
        }
        return {**profile, **changes}

    def envelope(self, **changes):
        return contract_envelope(
            self.profile(),
            run_id="run-1",
            modules=["registry", "financials", "website"],
            started_at="2026-08-24T00:00:00Z",
            completed_at="2026-08-24T00:00:05Z",
            **changes,
        )

    def test_envelope_carries_every_documented_field_and_validates(self):
        envelope = self.envelope()
        result = validate_contract_envelope(envelope)
        self.assertTrue(result["passed"], result["errors"])
        for key in ("contract_version", "organisation_number", "run", "claims", "evidence", "changes", "errors", "operations"):
            self.assertIn(key, envelope)
        self.assertEqual(envelope["contract_version"], CONTRACT_VERSION)
        self.assertEqual(envelope["run"]["terminal_status"], "completed")
        self.assertEqual(envelope["organisation_number"], "923609016")
        self.assertEqual(len(envelope["claims"]), len(envelope["evidence"]))
        self.assertEqual([claim["field"] for claim in envelope["claims"]], ["registry", "financials", "website"])
        self.assertEqual(envelope["changes"], [])
        self.assertEqual(envelope["operations"], {"requests": 2, "runtime_ms": 12, "third_party_cost_usd": 0.0})
        # legacy keys stay for existing consumers
        self.assertEqual(envelope["state"], "complete")
        self.assertEqual(envelope["run_id"], "run-1")

    def test_availability_vocabulary_is_documented_and_exhaustive(self):
        self.assertTrue(set(EVIDENCE_STATUS_TO_AVAILABILITY.values()) <= AVAILABILITY_STATES)
        self.assertEqual(EVIDENCE_STATUS_TO_AVAILABILITY["not_fetched"], "not_available")
        self.assertEqual(EVIDENCE_STATUS_TO_AVAILABILITY["blocked"], "blocked")
        self.assertEqual(EVIDENCE_STATUS_TO_AVAILABILITY["source_error"], "failed")
        self.assertEqual(availability_state(None), "not_available")
        envelope = self.envelope()
        by_field = {claim["field"]: claim["availability"] for claim in envelope["claims"]}
        self.assertEqual(by_field, {"registry": "available", "financials": "not_available", "website": "blocked"})
        self.assertEqual(envelope["errors"][0]["module"], "website")
        self.assertEqual(envelope["errors"][0]["state"], "blocked_robots")

    def test_blocked_module_does_not_cancel_a_legitimate_empty_output(self):
        profile = self.profile(evidence={})
        envelope = contract_envelope(
            profile,
            run_id="run-empty",
            modules=["registry", "website"],
            started_at="2026-08-24T00:00:00Z",
            completed_at="2026-08-24T00:00:01Z",
        )
        result = validate_contract_envelope(envelope)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual({claim["availability"] for claim in envelope["claims"]}, {"not_available"})
        self.assertEqual([record["content_sha256"] for record in envelope["evidence"]], [None, None])

    def test_validation_rejects_broken_claims_counts_and_operations(self):
        envelope = self.envelope()
        envelope["evidence"].append({"id": "ev-dangling", "source_url": None, "source_class": None, "retrieved_at": None, "content_sha256": None, "claim_span": "x"})
        result = validate_contract_envelope(envelope)
        self.assertFalse(result["passed"])
        self.assertIn("claims and evidence must contain the same number of records", result["errors"])

        envelope = self.envelope()
        envelope["claims"][0]["evidence_ids"] = ["ev-missing"]
        self.assertIn("claim registry references unknown evidence ev-missing", validate_contract_envelope(envelope)["errors"])

        envelope = self.envelope()
        envelope["claims"][0]["confidence"] = 1.5
        self.assertIn("claim registry has an invalid confidence", validate_contract_envelope(envelope)["errors"])

        envelope = self.envelope()
        envelope["claims"][0]["availability"] = "maybe"
        self.assertIn("claim registry has an unsupported availability state", validate_contract_envelope(envelope)["errors"])

        envelope = self.envelope()
        envelope["operations"]["requests"] = 99
        self.assertIn("operations.requests does not match profile.run_metrics.requests", validate_contract_envelope(envelope)["errors"])

        envelope = self.envelope()
        envelope["evidence"][0]["content_sha256"] = "not-a-hash"
        self.assertIn("evidence ev-registry has a malformed content_sha256", validate_contract_envelope(envelope)["errors"])

        envelope = self.envelope()
        del envelope["run"]["terminal_status"]
        self.assertIn("run is missing terminal_status", validate_contract_envelope(envelope)["errors"])

    def test_validation_rejects_external_leakage_and_organisation_mismatch(self):
        envelope = self.envelope()
        envelope["profile"]["evidence"]["external"] = {"accepted": 1}
        self.assertTrue(any("external data must not live inside profile.evidence" in item for item in validate_contract_envelope(envelope)["errors"]))

        gate = gate_observations(
            [{"id": "o1", "organisation_number": "987654321", "platform": "google_places", "signal_type": "place_summary",
              "source_url": "https://maps.example.invalid/place/x", "retrieved_at": "2026-08-20T00:00:00Z",
              "content_sha256": "a" * 64, "exact_entity": True, "identity_proof": [{"type": "proof"}],
              "acquisition_mode": "official_api", "rights_status": "approved", "source_class": "public_business_listing"}],
            organisation_numbers=["987654321"],
            as_of="2026-08-24T00:00:00Z",
        )
        block = company_external_block(gate, "987654321")
        self.assertEqual(block["organisation_number"], "987654321")
        envelope = self.envelope(external_block=block)  # envelope belongs to 923609016
        self.assertIn("external block organisation_number does not match the envelope", validate_contract_envelope(envelope)["errors"])

        envelope = self.envelope(external_block={**block, "publishable_only": False})
        self.assertIn("external block must be publishable-only", validate_contract_envelope(envelope)["errors"])

        envelope = self.envelope(external_block={**block, "accepted_observation_ids": [], "rejected_observations": [{"id": "x", "reasons": [], "value": "leak"}]})
        self.assertIn("rejected observations may only carry id and reasons", validate_contract_envelope(envelope)["errors"])

    def test_external_evidence_stays_out_of_official_claims_and_evidence(self):
        gate = gate_observations(
            [{"id": "o1", "organisation_number": "923609016", "platform": "google_places", "signal_type": "place_summary",
              "source_url": "https://maps.example.invalid/place/x", "retrieved_at": "2026-08-20T00:00:00Z",
              "content_sha256": "a" * 64, "exact_entity": True, "identity_proof": [{"type": "proof"}],
              "acquisition_mode": "official_api", "rights_status": "approved", "source_class": "public_business_listing"}],
            organisation_numbers=["923609016"],
            as_of="2026-08-24T00:00:00Z",
        )
        envelope = self.envelope(external_block=company_external_block(gate, "923609016"))
        self.assertTrue(validate_contract_envelope(envelope)["passed"], validate_contract_envelope(envelope)["errors"])
        self.assertNotIn("external", envelope["profile"]["evidence"])
        self.assertEqual(envelope["external"]["accepted_observation_ids"], ["o1"])
        self.assertEqual([claim["field"] for claim in envelope["claims"]], ["registry", "financials", "website"])
        self.assertEqual(
            {record["source_class"] for record in envelope["evidence"]},
            {"official_registry_bulk", "official_registry_api", "registry_linked_company_website"},
        )
        self.assertNotIn("maps.example.invalid", json.dumps(envelope["evidence"]))

    def test_terminal_envelope_wrapper_keeps_legacy_behaviour(self):
        envelope = terminal_envelope(
            self.profile(),
            run_id="run-legacy",
            modules=["registry"],
            started_at="2026-08-24T00:00:00Z",
            completed_at="2026-08-24T00:00:01Z",
        )
        self.assertEqual(envelope["state"], "complete")
        self.assertEqual(envelope["modules"]["registry"]["state"], "complete")
        self.assertIn("claims", envelope)
        self.assertTrue(validate_contract_envelope(envelope)["passed"])


class RemediationRegressionTests(unittest.TestCase):
    """Regressions for the four priority findings fixed in the 2026-10-02 remediation sprint.

    Every test names the finding it guards and asserts behaviour that was wrong before the fix
    (or that the fix introduced); the F1 test reruns the original two-invocation reproduction.
    """

    FIXTURES = ROOT / "tests" / "fixtures"

    def observation(self, **changes):
        base = {
            "id": "obs-1",
            "organisation_number": "923609016",
            "platform": "google_places",
            "signal_type": "place_summary",
            "source_url": "https://maps.example.invalid/place/example",
            "retrieved_at": "2026-08-20T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "synthetic_identity_proof"}],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "public_business_listing",
        }
        return {**base, **changes}

    def batch_run(self, workdir, label, extra):
        command = [
            sys.executable, str(ROOT / "scripts" / "run_competition_batch.py"),
            "--organisations", str(self.FIXTURES / "batch-orgs-3.jsonl"),
            "--bulk", str(self.FIXTURES / "bulk-registry-sample.csv.gz"),
            "--profiles-output", str(workdir / "profiles.jsonl"),
            "--output", str(workdir / f"envelopes-{label}.jsonl"),
            "--report", str(workdir / f"report-{label}.json"),
            "--run-id", f"regression-{label}",
            "--expected-count", "3",
            "--modules", "registry,accounting_obligation,website",
            "--external-as-of", "2026-08-24T00:00:00Z",
        ] + extra
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180)
        envelopes_path = workdir / f"envelopes-{label}.jsonl"
        report_path = workdir / f"report-{label}.json"
        envelopes = [
            json.loads(line) for line in envelopes_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ] if envelopes_path.exists() else []
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        return completed, envelopes, report

    # ------------------------------------------------------------------ F1
    def test_f1_resume_never_republishes_stale_external_evidence(self):
        """Original defect: a resumed run with no observations reused a stored external block."""
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            valid = str(self.FIXTURES / "external-observations-valid.jsonl")

            first, first_envelopes, first_report = self.batch_run(workdir, "first", ["--external-observations", valid])
            self.assertEqual(first.returncode, 0, first.stderr[-400:])
            self.assertEqual(first_report["contract_status"]["external_block_present"], 2)
            self.assertTrue(any("external_footprint" in envelope.get("profile", {}) for envelope in first_envelopes))

            second, second_envelopes, second_report = self.batch_run(workdir, "second", ["--resume"])
            self.assertEqual(second.returncode, 0, second.stderr[-400:])
            self.assertNotIn("external", second_report)
            self.assertEqual(second_report["contract_status"]["external_block_present"], 0)
            self.assertEqual(second_report["resume"]["profiles_reused"], 3)
            self.assertEqual(second_report["resume"]["stale_external_blocks_discarded"], 2)
            self.assertIn("never reused", second_report["resume"]["policy"])
            for envelope in second_envelopes:
                self.assertNotIn("external", envelope)
                self.assertNotIn("external_footprint", envelope["profile"])

            third, third_envelopes, third_report = self.batch_run(workdir, "third", ["--resume", "--external-observations", valid])
            self.assertEqual(third.returncode, 0, third.stderr[-400:])
            rebuilt = sorted(
                observation_id for envelope in third_envelopes
                for observation_id in (envelope.get("external") or {}).get("accepted_observation_ids", [])
            )
            self.assertIn("places-summary-923609016", rebuilt)
            self.assertEqual(third_report["contract_status"]["external_block_present"], 2)

    def test_f1_contract_validator_refuses_a_stored_block_without_a_matching_external_block(self):
        profile = {
            "organisation_number": "923609016",
            "name": "AF GRUPPEN ASA",
            "evidence": {"registry": evidence("registry", "available", "official_registry_bulk", "https://data.brreg.no/x", value={})},
            "run_metrics": {"requests": 1, "runtime_ms": 1},
            "external_footprint": {"organisation_number": "923609016", "accepted_observation_ids": ["o1"]},
        }
        envelope = contract_envelope(
            profile, run_id="run-stale", modules=["registry"],
            started_at="2026-08-24T00:00:00Z", completed_at="2026-08-24T00:00:01Z",
        )
        result = validate_contract_envelope(envelope)
        self.assertFalse(result["passed"])
        self.assertIn("profile.external_footprint is present but the envelope has no external block", result["errors"])

        inside = contract_envelope(
            profile, run_id="run-inside", modules=["registry"],
            started_at="2026-08-24T00:00:00Z", completed_at="2026-08-24T00:00:01Z",
            external_block={"organisation_number": "923609016", "publishable_only": True,
                            "accepted_observation_ids": ["o2"], "rejected_observations": []},
        )
        result = validate_contract_envelope(inside)
        self.assertFalse(result["passed"])
        self.assertIn("profile.external_footprint does not match the envelope external block", result["errors"])

    # ------------------------------------------------------------------ F2
    def test_f2_malformed_metrics_are_rejected_with_specific_reasons(self):
        cases = (
            ("not-a-dict", "metrics must be an object"),
            ([1, 2], "metrics must be an object"),
            ({"likes": "many"}, "metric likes must be a non-negative number"),
            ({"likes": [1]}, "metric likes must be a non-negative number"),
            ({"likes": {"value": 1}}, "metric likes must be a non-negative number"),
            ({"likes": True}, "metric likes must be a non-negative number"),
            ({"likes": -1}, "metric likes must be a non-negative number"),
            ({"likes": float("nan")}, "metric likes must be a non-negative number"),
        )
        for value, expected in cases:
            with self.subTest(metrics=value):
                self.assertIn(expected, validate_observation(self.observation(metrics=value)))
        self.assertEqual(validate_observation(self.observation(metrics={"likes": 4.5, "comments": 0})), [])
        self.assertEqual(validate_observation(self.observation(metrics=None)), [])

    def test_f2_unhashable_values_are_rejected_instead_of_raising(self):
        cases = {
            "platform": ["google_places"],
            "signal_type": {"type": "place_summary"},
            "acquisition_mode": ["official_api"],
            "sentiment_label": ["positive"],
            "source_class": {"name": "public_news"},
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                item = self.observation(signal_type="review", evidence_span="span", sentiment_label="positive",
                                        source_class="public_news", sentiment_model_version="model-1")
                item[field] = value
                reasons = validate_observation(item)
                self.assertTrue(reasons)
                gate = gate_observations([item], organisation_numbers=["923609016"])
                self.assertEqual(gate["accepted"], [])
                self.assertEqual(len(gate["rejected"]), 1)

    def test_f2_one_malformed_record_does_not_abort_the_batch(self):
        malformed = self.observation(id="bad", signal_type="public_post", metrics="not-a-dict")
        gate = gate_observations([malformed, self.observation()], organisation_numbers=["923609016"])
        self.assertEqual([item["id"] for item in gate["accepted"]], ["obs-1"])
        self.assertEqual(len(gate["rejected"]), 1)
        self.assertIn("metrics must be an object", gate["rejected"][0]["reasons"])

    def test_f2_identity_proof_and_evidence_span_are_typed(self):
        self.assertIn("missing exact-entity proof", validate_observation(self.observation(identity_proof={"type": "x"})))
        self.assertIn("identity proof entries must be objects", validate_observation(self.observation(identity_proof=["x"])))
        self.assertIn("missing evidence span", validate_observation(self.observation(signal_type="public_post", evidence_span=7)))
        self.assertIn("missing evidence span", validate_observation(self.observation(signal_type="public_post", evidence_span="  ")))

    def test_f2_invalid_timestamps_are_typed_or_counted_stale(self):
        self.assertIn("retrieval time must be a string", validate_observation(self.observation(retrieved_at=20260820)))
        undated = self.observation(**{"id": "undated", "retrieved_at": "not-a-date"})
        self.assertEqual(validate_observation(undated), [])
        gate = gate_observations([undated], organisation_numbers=["923609016"], as_of="2026-08-24T00:00:00Z")
        block = company_external_block(gate, "923609016")
        self.assertEqual(len(gate["accepted"]), 1)
        self.assertEqual(block["stale_observation_ids"], ["undated"])
        self.assertEqual(block["footprint"]["fresh_observations"], 0)

    def test_f2_malformed_org_numbers_are_data_errors_not_unmatched(self):
        malformed = self.observation(**{"id": "short-org", "organisation_number": "12345678"})
        coerced = self.observation(**{"id": "int-org", "organisation_number": 923609016})
        outsider = self.observation(**{"id": "unlisted", "organisation_number": "999999999"})
        gate = gate_observations([malformed, coerced, outsider], organisation_numbers=["923609016"])
        rejected = {entry["id"]: entry["reasons"] for entry in gate["rejected"]}
        self.assertIn("missing or invalid organisation number", rejected["short-org"])
        self.assertIn("missing or invalid organisation number", rejected["int-org"])
        self.assertEqual([entry["id"] for entry in gate["unmatched"]], ["unlisted"])

    # ------------------------------------------------------------------ F3
    def test_f3_audit_labels_are_bound_to_the_composite_identity(self):
        records = [
            self.observation(**{"id": "shared", "organisation_number": "923609016", "source_url": "https://maps.example.invalid/place/a"}),
            self.observation(**{"id": "shared", "organisation_number": "987654321", "source_url": "https://maps.example.invalid/place/b"}),
        ]
        labels = [{"organisation_number": "923609016", "id": "shared", "exact_entity": True, "metric_correct": True}]
        audit = audit_records(records, labels, organisation_numbers=["923609016", "987654321"])
        self.assertEqual(audit["published_audited"], 1)
        self.assertEqual(audit["labels_without_observation"], [])
        self.assertEqual(audit["observations_without_labels"], [["987654321", "shared"]])
        self.assertEqual(audit["coverage_by_organisation"]["987654321"]["published_audited"], 0)

    def test_f3_duplicate_labels_collapse_and_conflicting_labels_are_withheld(self):
        base = {"organisation_number": "923609016", "id": "obs-1", "exact_entity": True, "metric_correct": True}
        duplicate = audit_records([self.observation()], [base, dict(base)], organisation_numbers=["923609016"])
        self.assertEqual(duplicate["published_audited"], 1)
        self.assertEqual([entry["position"] for entry in duplicate["labels_duplicate"]], [2])
        self.assertEqual(duplicate["labels_conflicting"], [])

        conflict = audit_records([self.observation()], [base, {**base, "exact_entity": False}], organisation_numbers=["923609016"])
        self.assertEqual(len(conflict["labels_conflicting"]), 1)
        self.assertEqual(conflict["labels_usable"], 0)
        self.assertEqual(conflict["published_audited"], 0)
        self.assertFalse(conflict["qualification_passed"])

    def test_f3_diagnostics_cover_unmatched_and_unlabelled(self):
        labels = [
            {"organisation_number": "923609016", "id": "ghost", "exact_entity": True, "metric_correct": True},
            {"id": "no-org", "exact_entity": True},
        ]
        audit = audit_records([self.observation()], labels, organisation_numbers=["923609016"])
        self.assertEqual(audit["labels_without_observation"], [["923609016", "ghost"]])
        self.assertEqual(audit["observations_without_labels"], [["923609016", "obs-1"]])
        self.assertEqual(len(audit["labels_invalid"]), 1)
        self.assertEqual(audit["labels_invalid"][0]["reason"], "label needs a string organisation_number and id")

    # ------------------------------------------------------------------ F4
    def test_f4_scorer_cannot_close_the_audit_gate_with_fabricated_report_values(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            profiles = workdir / "profiles.jsonl"
            profiles.write_text(
                "\n".join(json.dumps({"organisation_number": org}) for org in ("923609016", "987654321", "123456789")) + "\n",
                encoding="utf-8",
            )
            fabricated = {
                "qualification_passed": True,
                "audit_size_gate": True,
                "published_audited": 500,
                "wrong_entity_publications": 0,
                "unsupported_publications": 0,
                "fresh_coverage": 1.0,
                "connector_policy_passed": True,
                "coverage": {key: 1.0 for key in (
                    "any_external", "two_platforms", "workforce_jobs", "ratings_reviews", "buzz_engagement", "sentiment")},
            }
            external = workdir / "external.json"
            external.write_text(json.dumps(fabricated), encoding="utf-8")
            for name in ("batch", "refresh", "research", "ux"):
                (workdir / f"{name}.json").write_text("{}", encoding="utf-8")
            output = workdir / "score.json"
            command = [
                sys.executable, str(ROOT / "scripts" / "score_competition_v3.py"),
                "--profiles", str(profiles),
                "--external-report", str(external),
                "--batch-report", str(workdir / "batch.json"),
                "--refresh-report", str(workdir / "refresh.json"),
                "--research-report", str(workdir / "research.json"),
                "--ux-report", str(workdir / "ux.json"),
                "--output", str(output),
            ]
            untrusted = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(untrusted.returncode, 0, untrusted.stderr[-400:])
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["measurement_source"], "unverified_report")
            self.assertFalse(report["measurement_trust"]["report_values_explicitly_trusted"])
            # Every external gate must stay unproven in report-only mode: a future edit that made
            # report trust implicit (or trusted the counts while leaving the audit gate alone)
            # would turn one of these true and this test would fail.
            for gate in (
                "external_audit_at_least_100",
                "zero_wrong_company_external_publications",
                "external_claims_supported",
                "external_connector_policy",
            ):
                with self.subTest(gate=gate):
                    self.assertFalse(report["qualification_gates"][gate], gate)
            # Untrusted report values must not earn a single external point, not even in the
            # would-be raw score: dropping the report_trusted guard from external_qualified would
            # light these components up and this assertion would fail.
            self.assertEqual(set(report["details"]["external"].values()), {0.0})
            self.assertEqual(report["category_scores"]["external_footprint_intelligence"], 0.0)
            self.assertEqual(report["awardable_score"], 0)

            derived = subprocess.run(
                command + ["--observations", str(self.FIXTURES / "external-observations-valid.jsonl"),
                           "--labels", str(self.FIXTURES / "external-audit-labels.synthetic.jsonl")],
                cwd=ROOT, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(derived.returncode, 0, derived.stderr[-400:])
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["measurement_source"], "derived_from_artifacts")
            self.assertFalse(report["qualification_gates"]["external_report_consistent"])
            self.assertTrue(report["external_report_mismatches"])
            self.assertEqual(report["awardable_score"], 0)


class IntegrityHardeningTests(unittest.TestCase):
    """Regressions for the second remediation pass (S1-S6 contract and input integrity).

    Each test targets behaviour that was wrong on the frozen tree before this pass: malformed claim
    fields raised TypeError out of the validator, external counts were never cross-checked against
    the ids they summarise, and the timestamps, organisation numbers, claim values and non-UTF-8
    inputs listed below were accepted or crashed.
    """

    def observation(self, **changes):
        base = {
            "id": "obs-1",
            "organisation_number": "923609016",
            "platform": "google_places",
            "signal_type": "place_summary",
            "source_url": "https://maps.example.invalid/place/example",
            "retrieved_at": "2026-08-20T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "synthetic_identity_proof"}],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "public_business_listing",
        }
        return {**base, **changes}

    def profile(self, **changes):
        profile = {
            "organisation_number": "923609016",
            "name": "AF GRUPPEN ASA",
            "evidence": {
                "registry": evidence("registry", "available", "official_registry_bulk", "https://data.brreg.no/x", value={"navn": "AF GRUPPEN ASA"}),
                "financials": evidence("financials", "not_found", "official_registry_api", "https://data.brreg.no/y", note="No annual accounts"),
                "website": evidence("website", "blocked", "registry_linked_company_website", "https://afgruppen.no/", note="robots.txt disallowed"),
            },
            "run_metrics": {"requests": 2, "runtime_ms": 12},
        }
        return {**profile, **changes}

    def envelope(self, **changes):
        return contract_envelope(
            self.profile(),
            run_id="run-hardening",
            modules=["registry", "financials", "website"],
            started_at="2026-08-24T00:00:00Z",
            completed_at="2026-08-24T00:00:05Z",
            **changes,
        )

    def external_block(self, *, accepted=1, rejected=1, stale=0, count_scope="accepted"):
        """A hand-built block that satisfies every cross-check, for tampering in tests."""
        accepted_ids = [f"a{i}" for i in range(1, accepted + 1)]
        rejected_entries = [{"id": f"r{i}", "reasons": ["source rights are not approved"]} for i in range(1, rejected + 1)]
        fresh = accepted - stale
        return {
            "organisation_number": "923609016",
            "publishable_only": True,
            "counts_scope": count_scope,
            "accepted_observation_ids": accepted_ids,
            "stale_observation_ids": accepted_ids[-stale:] if stale else [],
            "rejected_observations": rejected_entries,
            "footprint": {
                "status": "available" if accepted else "not_available",
                "accepted_observations": accepted,
                "counted_observations": fresh if count_scope == "fresh" else accepted,
                "counts_scope": count_scope,
                "rejected_observations": rejected,
                "rejections": rejected_entries,
                "fresh_observations": fresh,
                "stale_observations": stale,
                "undated_observations": 0,
            },
        }

    # ------------------------------------------------------------------ S1
    def test_s1_malformed_claim_field_types_return_errors_instead_of_raising(self):
        for bad in (None, {"a": 1}, 7, 2.5, True, "", "   "):
            with self.subTest(field=bad):
                envelope = self.envelope()
                envelope["claims"][0]["field"] = bad
                result = validate_contract_envelope(envelope)
                self.assertFalse(result["passed"])
                self.assertIn("claim #1 field must be a non-empty string", result["errors"])

        envelope = self.envelope()
        del envelope["claims"][0]["field"]
        result = validate_contract_envelope(envelope)
        self.assertIn("claim #1 field must be a non-empty string", result["errors"])

    def test_s1_one_malformed_claim_does_not_cancel_validation_of_the_rest(self):
        envelope = self.envelope()
        envelope["claims"][0]["field"] = None
        envelope["claims"][1]["confidence"] = 1.5
        result = validate_contract_envelope(envelope)
        self.assertIn("claim #1 field must be a non-empty string", result["errors"])
        # The second claim is still validated in full, under its own valid label.
        self.assertIn("claim financials has an invalid confidence", result["errors"])
        # No TypeError, and no misleading module-coverage error caused by the untyped field.
        self.assertNotIn("claims must cover exactly the requested modules", result["errors"])

    def test_s1_valid_claims_still_cover_the_requested_modules(self):
        self.assertTrue(validate_contract_envelope(self.envelope())["passed"])
        envelope = self.envelope()
        envelope["modules"].pop("website")
        result = validate_contract_envelope(envelope)
        self.assertIn("claims must cover exactly the requested modules", result["errors"])

    def test_s1_non_string_module_names_do_not_raise(self):
        envelope = self.envelope()
        envelope["modules"][7] = envelope["modules"].pop("website")
        result = validate_contract_envelope(envelope)
        self.assertFalse(result["passed"])
        self.assertIn("module names must be strings", result["errors"])

    # ------------------------------------------------------------------ S2
    def test_s2_matching_counts_pass_and_mismatched_counts_are_rejected(self):
        consistent = self.envelope(external_block=self.external_block(accepted=2, rejected=1))
        self.assertTrue(validate_contract_envelope(consistent)["passed"], validate_contract_envelope(consistent)["errors"])

        for field, value, expected in (
            ("accepted_observations", 999, "does not match the accepted observation ids (2)"),
            ("stale_observations", 1, "does not match the stale observation ids (0)"),
            ("rejected_observations", 0, "does not match the rejection diagnostics (1)"),
        ):
            with self.subTest(field=field):
                envelope = self.envelope(external_block=self.external_block(accepted=2, rejected=1))
                envelope["external"]["footprint"][field] = value
                result = validate_contract_envelope(envelope)
                self.assertFalse(result["passed"])
                self.assertTrue(any(expected in item for item in result["errors"]), result["errors"])

    def test_s2_duplicate_accepted_ids_cannot_inflate_a_count(self):
        envelope = self.envelope(external_block=self.external_block(accepted=2, rejected=0))
        block = envelope["external"]
        block["accepted_observation_ids"] = ["a1", "a1"]
        block["footprint"]["accepted_observations"] = 2
        block["footprint"]["fresh_observations"] = 2
        result = validate_contract_envelope(envelope)
        self.assertFalse(result["passed"])
        self.assertIn("external accepted_observation_ids must not repeat an identifier", result["errors"])

    def test_s2_aggregation_policy_invariants_are_enforced(self):
        envelope = self.envelope(external_block=self.external_block(accepted=3, rejected=0, stale=1))
        envelope["external"]["footprint"]["accepted_observations"] = 4  # 4 != fresh(2) + stale(1)
        result = validate_contract_envelope(envelope)
        self.assertIn("external footprint accepted_observations must equal fresh + stale", result["errors"])

        envelope = self.envelope(external_block=self.external_block(accepted=2, rejected=0, stale=0, count_scope="fresh"))
        envelope["external"]["footprint"]["counted_observations"] = 2  # must follow counts_scope
        envelope["external"]["footprint"]["fresh_observations"] = 1
        result = validate_contract_envelope(envelope)
        self.assertTrue(any("counted_observations must equal" in item for item in result["errors"]), result["errors"])

    def test_s2_missing_footprint_and_dangling_ids_are_rejected(self):
        envelope = self.envelope(external_block=self.external_block())
        envelope["external"].pop("footprint")
        result = validate_contract_envelope(envelope)
        self.assertFalse(result["passed"])
        self.assertIn("external block must carry a footprint object with its aggregate counts", result["errors"])

        envelope = self.envelope(external_block=self.external_block())
        envelope["external"]["stale_observation_ids"] = ["never-accepted"]
        result = validate_contract_envelope(envelope)
        self.assertIn("external stale observation ids must also be accepted observation ids", result["errors"])

        envelope = self.envelope(external_block=self.external_block(accepted=1, rejected=0))
        envelope["external"]["rejected_observations"] = [{"id": "a1", "reasons": ["x"]}]
        envelope["external"]["footprint"]["rejected_observations"] = 1
        envelope["external"]["footprint"]["rejections"] = [{"id": "a1", "reasons": ["x"]}]
        result = validate_contract_envelope(envelope)
        self.assertIn("observations cannot be both accepted and rejected: ['a1']", result["errors"])

    def test_s2_empty_and_absent_external_evidence_stay_valid(self):
        empty = self.envelope(external_block=self.external_block(accepted=0, rejected=0))
        self.assertTrue(validate_contract_envelope(empty)["passed"], validate_contract_envelope(empty)["errors"])
        self.assertEqual(empty["external"]["footprint"]["status"], "not_available")
        self.assertTrue(validate_contract_envelope(self.envelope())["passed"])

    def test_s2_real_gate_block_satisfies_every_cross_check(self):
        records = [
            self.observation(**{"id": "keep"}),
            self.observation(**{"id": "drop-rights", "rights_status": "review_required"}),
            self.observation(**{"id": "drop-dupe", "source_url": "https://maps.example.invalid/place/dupe"}),
            self.observation(**{"id": "drop-dupe", "source_url": "https://maps.example.invalid/place/dupe-2"}),
        ]
        gate = gate_observations(records, organisation_numbers=["923609016"], as_of="2026-08-24T00:00:00Z")
        block = company_external_block(gate, "923609016")
        envelope = self.envelope(external_block=block)
        result = validate_contract_envelope(envelope)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(len(block["accepted_observation_ids"]), block["footprint"]["accepted_observations"])
        self.assertEqual(len(block["rejected_observations"]), block["footprint"]["rejected_observations"])
        self.assertEqual(len(block["footprint"]["rejections"]), block["footprint"]["rejected_observations"])

    # ------------------------------------------------------------------ S3
    def test_s3_claims_without_data_must_not_carry_a_value(self):
        envelope = self.envelope()
        for claim in envelope["claims"]:
            if claim["field"] == "financials":
                claim["value"] = {"profit": 1}
        result = validate_contract_envelope(envelope)
        self.assertFalse(result["passed"])
        self.assertIn("claim financials must not carry a value when availability is not_available", result["errors"])

        available = self.envelope()
        self.assertIsNotNone(available["claims"][0]["value"])
        self.assertTrue(validate_contract_envelope(available)["passed"])

    # ------------------------------------------------------------------ S4
    def test_s4_non_utf8_observation_file_raises_a_structured_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-utf8.jsonl"
            path.write_bytes(b'{"id": "x", "organisation_number": "\xc3\x28"}\n')
            with self.assertRaises(ObservationInputError) as raised:
                read_observation_file(path)
            message = str(raised.exception)
            self.assertIn("Cannot read external observation file", message)
            self.assertIn("not valid UTF-8", message)
            self.assertIn(str(path), message)

            decodable = Path(directory) / "mixed.jsonl"
            decodable.write_text('{"id": "ok"}\nnot json\n', encoding="utf-8")
            records, malformed = read_observation_file(decodable)
            self.assertEqual([record["id"] for record in records], ["ok"])
            self.assertEqual(len(malformed), 1)

    # ------------------------------------------------------------------ S5
    def test_s5_run_timestamps_must_be_iso_8601(self):
        for bad in ("yesterday", "2026-13-45T99:99:99Z", "24/08/2026"):
            with self.subTest(value=bad):
                envelope = self.envelope()
                envelope["run"]["started_at"] = bad
                result = validate_contract_envelope(envelope)
                self.assertFalse(result["passed"])
                self.assertIn("run.started_at must be an ISO-8601 timestamp", result["errors"])

        envelope = self.envelope()
        envelope["run"]["started_at"] = 20260824
        result = validate_contract_envelope(envelope)
        self.assertIn("run.started_at must be a non-empty string", result["errors"])

        self.assertIsNotNone(parse_timestamp("2026-08-24T00:00:00Z"))
        self.assertTrue(validate_contract_envelope(self.envelope())["passed"])

    # ------------------------------------------------------------------ S6
    def test_s6_organisation_number_policy_is_shared_everywhere(self):
        self.assertIs(is_organisation_number, _is_organisation_number)
        for bad in ("１２３４５６７８９", "01234567", "12345678", "1234567890", 923609016, "923609016 "):
            with self.subTest(value=bad):
                self.assertFalse(is_organisation_number(bad))
                envelope = self.envelope()
                envelope["organisation_number"] = bad
                result = validate_contract_envelope(envelope)
                self.assertIn("organisation_number must be exactly nine ASCII digits", result["errors"])
                # The observation gate applies exactly the same policy.
                self.assertIn("missing or invalid organisation number", validate_observation(self.observation(organisation_number=bad)))
        self.assertTrue(is_organisation_number("923609016"))
        self.assertTrue(validate_contract_envelope(self.envelope())["passed"])

    # ------------------------------------------------------------------ S8
    # ------------------------------------------------------------------ S7
    def test_s7_undocumented_evidence_fields_are_rejected(self):
        self.assertTrue(validate_contract_envelope(self.envelope())["passed"])

        envelope = self.envelope()
        envelope["evidence"][0]["internal_debug"] = {"trace": "x"}
        result = validate_contract_envelope(envelope)
        self.assertFalse(result["passed"])
        self.assertTrue(
            any("undocumented fields" in error and "internal_debug" in error for error in result["errors"]),
            result["errors"],
        )

        # The check is an allow-list, so an internal field cannot escape it by being renamed.
        envelope = self.envelope()
        envelope["evidence"][0]["debug_trace"] = 1
        result = validate_contract_envelope(envelope)
        self.assertFalse(result["passed"])
        self.assertTrue(any("debug_trace" in error for error in result["errors"]), result["errors"])

        # A legitimately empty output (no modules requested) stays valid.
        envelope = self.envelope()
        envelope["modules"] = {}
        envelope["evidence"] = []
        envelope["claims"] = []
        self.assertTrue(validate_contract_envelope(envelope)["passed"])

    def test_s8_non_finite_measurements_earn_no_credit(self):
        self.assertIsNone(numeric(float("nan")))
        self.assertIsNone(numeric(float("inf")))
        self.assertIsNone(numeric(float("-inf")))
        self.assertIsNone(numeric(True))
        self.assertEqual(numeric(0.5), 0.5)
        self.assertEqual(capped(10, numeric(float("nan"))), 0.0)
        self.assertEqual(capped(10, numeric(float("inf"))), 0.0)
        self.assertEqual(capped(10, 1.0), 10.0)

    # ------------------------------------------------------------------ S9
    def test_s9_rejection_diagnostics_carry_json_safe_ids(self):
        self.assertEqual(diagnostic_id("abc"), "abc")
        self.assertIsNone(diagnostic_id(None))
        self.assertEqual(diagnostic_id(["a", "b"]), '["a", "b"]')

        gate = gate_observations(
            [
                self.observation(**{"id": ["a", "b"]}),
                self.observation(**{"id": "keep"}),
            ],
            organisation_numbers=["923609016"],
        )
        block = company_external_block(gate, "923609016")
        ids = [entry["id"] for entry in block["rejected_observations"]]
        self.assertIn('["a", "b"]', ids)
        self.assertTrue(all(value is None or isinstance(value, str) for value in ids))
        self.assertTrue(validate_contract_envelope(self.envelope(external_block=block))["passed"])


class AuditEligibilityTests(unittest.TestCase):
    """Findings A/B/C: only distinct, conflict-free, in-batch, policy-accepted observations audit.

    Each case reproduces a defect that was present on commit c0d557e: duplicate rows inflated
    published_audited (A), out-of-batch rows inflated it (B), and every raw-input path could re-open
    the gate because audit_records() trusted its caller (C).
    """

    ORG_A = "923609016"
    ORG_B = "987654321"

    def observation(self, org, observation_id, **changes):
        import hashlib as _hashlib
        base = {
            "id": observation_id,
            "organisation_number": org,
            "platform": "google_places",
            "signal_type": "place_summary",
            "source_url": f"https://maps.example.invalid/place/{observation_id}",
            "retrieved_at": "2026-08-20T00:00:00Z",
            "content_sha256": _hashlib.sha256(observation_id.encode()).hexdigest(),
            "exact_entity": True,
            "identity_proof": [{"type": "synthetic_identity_proof"}],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "public_business_listing",
        }
        return {**base, **changes}

    def label(self, org, observation_id, **changes):
        return {"organisation_number": org, "id": observation_id, "exact_entity": True, "metric_correct": True, **changes}

    def distinct(self, org, count, prefix="distinct", **changes):
        return [self.observation(org, f"{prefix}-{index:03d}", **changes) for index in range(count)]

    def distinct_labels(self, org, count, prefix="distinct", **changes):
        return [self.label(org, f"{prefix}-{index:03d}", **changes) for index in range(count)]

    def audit(self, observations, labels, batch, minimum=100):
        return audit_records(observations, labels, minimum_audit=minimum, organisation_numbers=batch)

    # ---------------------------------------------------------------- Finding A
    def test_a1_one_observation_repeated_100_times_cannot_qualify(self):
        audit = self.audit([self.observation(self.ORG_A, "dup") for _ in range(100)],
                           [self.label(self.ORG_A, "dup")], [self.ORG_A])
        self.assertEqual(audit["published_audited"], 1)
        self.assertEqual(audit["audit_size"], 1)
        self.assertFalse(audit["audit_size_gate"])
        self.assertFalse(audit["qualification_passed"])
        self.assertEqual(audit["eligibility"]["collapsed_identical"], 99)

    def test_a2_99_copies_plus_one_distinct_counts_two(self):
        records = [self.observation(self.ORG_A, "dup") for _ in range(99)] + [self.observation(self.ORG_A, "other")]
        audit = self.audit(records, [self.label(self.ORG_A, "dup"), self.label(self.ORG_A, "other")], [self.ORG_A])
        self.assertEqual(audit["published_audited"], 2)
        self.assertFalse(audit["qualification_passed"])

    def test_a3_the_gate_still_opens_for_100_distinct_records(self):
        audit = self.audit(self.distinct(self.ORG_A, 100), self.distinct_labels(self.ORG_A, 100), [self.ORG_A])
        self.assertEqual(audit["published_audited"], 100)
        self.assertTrue(audit["audit_size_gate"])
        self.assertTrue(audit["qualification_passed"])

    def test_a4_conflicting_payloads_for_one_identity_are_withheld(self):
        conflict = [
            self.observation(self.ORG_A, "conf"),
            self.observation(self.ORG_A, "conf", content_sha256="f" * 64,
                             source_url="https://maps.example.invalid/place/conflict"),
        ]
        audit = self.audit(conflict, [self.label(self.ORG_A, "conf")], [self.ORG_A], minimum=2)
        self.assertEqual(audit["published_audited"], 0)
        self.assertFalse(audit["qualification_passed"])
        self.assertEqual(len(audit["eligibility"]["conflicting_groups"]), 1)
        self.assertEqual(audit["eligibility"]["withheld_conflict_records"], 2)
        self.assertEqual([entry["status"] for entry in audit["eligibility"]["classifications"]],
                         ["withheld_conflict", "withheld_conflict"])
        # The label stays usable but can never be applied to one arbitrary payload of the group; the
        # withheld identity is reported as labelled evidence that did not count.
        self.assertEqual(audit["labels_usable"], 1)
        self.assertEqual(audit["audited_unpublished"], [["923609016", "conf"]])

    def test_a5_json_key_order_is_not_a_conflict(self):
        reordered = dict(reversed(list(self.observation(self.ORG_A, "distinct-000").items())))
        records = self.distinct(self.ORG_A, 100) + [reordered]
        audit = self.audit(records, self.distinct_labels(self.ORG_A, 100), [self.ORG_A])
        self.assertEqual(audit["published_audited"], 100)
        self.assertEqual(audit["eligibility"]["conflicting_groups"], [])
        self.assertEqual(audit["eligibility"]["collapsed_identical"], 1)

    def test_a6_duplicate_labels_collapse_and_conflicting_labels_withhold_an_identity(self):
        labels = (self.distinct_labels(self.ORG_A, 100)
                  + [self.label(self.ORG_A, "distinct-000")]                        # identical duplicate label
                  + [self.label(self.ORG_A, "distinct-001", metric_correct=False),  # conflicting label pair
                     self.label(self.ORG_A, "distinct-001", metric_correct=True)])
        audit = self.audit(self.distinct(self.ORG_A, 100), labels, [self.ORG_A])
        # Identical duplicate labels collapse harmlessly; the one conflicting identity is withheld,
        # so exactly one of the 100 labelled observations stops counting.
        self.assertEqual(audit["published_audited"], 99)
        self.assertEqual(audit["labels_usable"], 99)
        self.assertGreaterEqual(len(audit["labels_duplicate"]), 1)
        self.assertEqual(len(audit["labels_conflicting"]), 1)
        self.assertTrue(all(entry["identity"] != ["923609016", "distinct-001"] for entry in audit["labels_usable"].values()) if isinstance(audit["labels_usable"], dict) else True)
        self.assertFalse(audit["qualification_passed"])

    def test_a7_duplicates_cannot_inflate_precision_denominators(self):
        records = [self.observation(self.ORG_A, "dup") for _ in range(100)]
        labels = [self.label(self.ORG_A, "dup", exact_entity=False, metric_correct=False)]
        audit = self.audit(records, labels, [self.ORG_A], minimum=1)
        self.assertEqual(audit["published_audited"], 1)
        self.assertEqual(audit["wrong_entity_publications"], 1)
        self.assertEqual(audit["entity_precision"], 0.0)

    # ---------------------------------------------------------------- Finding B
    def test_b1_observations_for_another_organisation_cannot_qualify_a_batch(self):
        audit = self.audit(self.distinct(self.ORG_B, 100, "b"), self.distinct_labels(self.ORG_B, 100, "b"), [self.ORG_A])
        self.assertEqual(audit["published_audited"], 0)
        self.assertFalse(audit["qualification_passed"])
        self.assertEqual(audit["eligibility"]["out_of_batch_records"], 100)

    def test_b2_a_two_organisation_batch_audits_both(self):
        records = self.distinct(self.ORG_A, 100) + self.distinct(self.ORG_B, 100, "b")
        labels = self.distinct_labels(self.ORG_A, 100) + self.distinct_labels(self.ORG_B, 100, "b")
        audit = self.audit(records, labels, [self.ORG_A, self.ORG_B])
        self.assertEqual(audit["published_audited"], 200)
        self.assertTrue(audit["qualification_passed"])

    def test_b3_labels_for_another_batch_do_not_pull_records_into_scope(self):
        records = self.distinct(self.ORG_A, 5) + self.distinct(self.ORG_B, 5, "b")
        audit = self.audit(records, self.distinct_labels(self.ORG_B, 5, "b"), [self.ORG_A])
        self.assertEqual(audit["published_audited"], 0)
        self.assertFalse(audit["qualification_passed"])

    def test_b4_no_batch_keeps_the_documented_global_scope(self):
        audit = self.audit(self.distinct(self.ORG_B, 100, "b"), self.distinct_labels(self.ORG_B, 100, "b"), None)
        self.assertEqual(audit["published_audited"], 100)
        self.assertTrue(audit["qualification_passed"])
        self.assertFalse(audit["eligibility"]["batch_scoped"])

    def test_b5_an_empty_batch_can_never_qualify(self):
        audit = self.audit(self.distinct(self.ORG_A, 100), self.distinct_labels(self.ORG_A, 100), [])
        self.assertEqual(audit["published_audited"], 0)
        self.assertFalse(audit["qualification_passed"])
        self.assertTrue(audit["eligibility"]["batch_scoped"])

    def test_b6_mixed_evidence_counts_only_the_batch(self):
        records = self.distinct(self.ORG_A, 50) + self.distinct(self.ORG_B, 50, "b")
        labels = self.distinct_labels(self.ORG_A, 50) + self.distinct_labels(self.ORG_B, 50, "b")
        audit = self.audit(records, labels, [self.ORG_A])
        self.assertEqual(audit["published_audited"], 50)
        self.assertEqual(audit["eligibility"]["eligible_records"], 50)

    def test_b7_invalid_and_normalised_identifiers_are_data_errors_not_batch_misses(self):
        zero_padded = self.distinct("000923609016", 3, "norm")
        as_integer = [self.observation(self.ORG_A, f"int-{index}", organisation_number=int(self.ORG_A)) for index in range(3)]
        labels = self.distinct_labels("000923609016", 3, "norm") + [self.label(self.ORG_A, f"int-{index}") for index in range(3)]
        audit = self.audit(zero_padded + as_integer, labels, [self.ORG_A])
        self.assertEqual(audit["published_audited"], 0)
        self.assertEqual(audit["eligibility"]["out_of_batch_records"], 0)
        self.assertEqual(audit["eligibility"]["ineligible_records"], 6)

    def test_b8_out_of_batch_records_cannot_inflate_coverage(self):
        records = self.distinct(self.ORG_A, 2) + self.distinct(self.ORG_B, 100, "b")
        coverage = coverage_from_observations(records, organisation_numbers=[self.ORG_A, self.ORG_B],
                                              as_of="2026-08-24T00:00:00Z")
        self.assertEqual(coverage["any_external"], 1.0)
        only_a = coverage_from_observations(records, organisation_numbers=[self.ORG_A], as_of="2026-08-24T00:00:00Z")
        self.assertEqual(only_a["any_external"], 1.0)
        self.assertEqual(only_a["fresh"], 1.0)

    # ---------------------------------------------------------------- Finding C
    def test_c1_c3_records_that_fail_the_publication_policy_cannot_audit(self):
        cases = {
            "invalid organisation number": ([self.observation("12345", f"s-{i}") for i in range(3)], lambda oid: self.label("12345", oid)),
            "experimental acquisition mode": ([self.observation(self.ORG_A, f"e-{i}", acquisition_mode="unofficial_scraper_experiment") for i in range(3)], lambda oid: self.label(self.ORG_A, oid)),
            "unapproved rights": ([self.observation(self.ORG_A, f"r-{i}", rights_status="pending") for i in range(3)], lambda oid: self.label(self.ORG_A, oid)),
        }
        for name, (records, make_label) in cases.items():
            with self.subTest(case=name):
                audit = self.audit(records, [make_label(item["id"]) for item in records], [self.ORG_A])
                self.assertEqual(audit["published_audited"], 0, name)
                self.assertFalse(audit["qualification_passed"], name)

    def test_c4_out_of_batch_records_cannot_audit(self):
        audit = self.audit(self.distinct(self.ORG_B, 100, "b"), self.distinct_labels(self.ORG_B, 100, "b"), [self.ORG_A])
        self.assertEqual(audit["published_audited"], 0)

    def test_c5_identical_duplicates_collapse_before_auditing(self):
        audit = self.audit([self.observation(self.ORG_A, "dup") for _ in range(100)], [self.label(self.ORG_A, "dup")], [self.ORG_A])
        self.assertEqual(audit["published_audited"], 1)

    def test_c6_conflicting_duplicates_cannot_audit(self):
        conflict = [self.observation(self.ORG_A, "conf"),
                    self.observation(self.ORG_A, "conf", content_sha256="e" * 64,
                                     source_url="https://maps.example.invalid/place/elsewhere")]
        audit = self.audit(conflict, [self.label(self.ORG_A, "conf")], [self.ORG_A], minimum=1)
        self.assertEqual(audit["published_audited"], 0)
        self.assertFalse(audit["qualification_passed"])

    def test_c7_valid_accepted_records_still_audit(self):
        audit = self.audit(self.distinct(self.ORG_A, 100), self.distinct_labels(self.ORG_A, 100), [self.ORG_A])
        self.assertEqual(audit["published_audited"], 100)
        self.assertTrue(audit["qualification_passed"])

    def test_c8_stale_records_keep_the_documented_semantics(self):
        stale = self.distinct(self.ORG_A, 100, "stale", retrieved_at="2020-01-01T00:00:00Z")
        audit = self.audit(stale, self.distinct_labels(self.ORG_A, 100, "stale"), [self.ORG_A])
        self.assertEqual(audit["published_audited"], 100)
        self.assertTrue(audit["audit_size_gate"])
        self.assertTrue(audit["qualification_passed"])
        self.assertEqual(audit["eligibility"]["ineligible_records"], 0)

    def test_c9_raw_and_gated_inputs_produce_the_same_audit(self):
        """The core of Finding C: a closed gate cannot be re-opened through another path."""
        records = ([self.observation(self.ORG_A, "dup") for _ in range(3)]
                   + [self.observation(self.ORG_A, "valid")]
                   + [self.observation(self.ORG_B, "outside")]
                   + [self.observation("12345", "malformed-org")]
                   + [self.observation(self.ORG_A, "conf"), self.observation(self.ORG_A, "conf", content_sha256="d" * 64,
                                                                             source_url="https://maps.example.invalid/place/x")])
        labels = [self.label(self.ORG_A, name) for name in ("dup", "valid", "conf", "malformed-org")]
        gate = gate_observations(records, organisation_numbers=[self.ORG_A])
        from_raw = audit_records(records, labels, minimum_audit=1, organisation_numbers=[self.ORG_A])
        from_gate = audit_records(gate["accepted"], labels, minimum_audit=1, organisation_numbers=[self.ORG_A])
        from_unscoped_gate = audit_records(gate["accepted"], labels, minimum_audit=1, organisation_numbers=[self.ORG_A])
        # dup (3 copies -> 1) and valid are eligible and labelled; the conflict, the out-of-batch
        # record and the malformed identifier never count, whichever path supplies the rows.
        self.assertEqual(from_raw["published_audited"], 2)
        self.assertEqual(from_raw["published_audited"], from_gate["published_audited"])
        self.assertEqual(from_raw["published_audited"], from_unscoped_gate["published_audited"])
        self.assertEqual(from_raw["entity_precision"], from_gate["entity_precision"])
        self.assertEqual(from_gate["eligibility"]["eligible_records"], 2)
        # The raw path must do the withholding itself; the gated path is handed records from which the
        # gate has already removed the conflict, so the same two records remain the only ones audited.
        self.assertEqual(from_raw["eligibility"]["withheld_conflict_records"], 2)
        self.assertEqual(from_gate["eligibility"]["withheld_conflict_records"], 0)
        self.assertEqual(from_gate["eligibility"]["out_of_batch_records"], 0)

    def test_c10_eligible_selector_classifies_every_record_exactly_once(self):
        records = ([self.observation(self.ORG_A, "dup") for _ in range(2)]
                   + [self.observation(self.ORG_A, "valid")]
                   + [self.observation(self.ORG_B, "outside")]
                   + [self.observation(self.ORG_A, "conf"), self.observation(self.ORG_A, "conf", content_sha256="c" * 64,
                                                                             source_url="https://maps.example.invalid/place/y")])
        eligible, report = eligible_observations(records, organisation_numbers=[self.ORG_A])
        self.assertEqual([row["id"] for row in eligible], ["dup", "valid"])
        statuses = sorted(entry["status"] for entry in report["classifications"])
        self.assertEqual(statuses, ["eligible", "eligible", "out_of_batch", "withheld_conflict", "withheld_conflict"])
        self.assertEqual(report["input_records"], 6)
        self.assertEqual(report["unique_records"], 3)
        self.assertEqual(report["collapsed_identical"], 1)
        self.assertEqual(report["out_of_batch_records"], 1)
        self.assertEqual(report["withheld_conflict_records"], 2)
        self.assertEqual(report["eligible_records"], 2)
        self.assertTrue(report["batch_scoped"])
        withheld = [entry for entry in report["classifications"] if entry["status"] == "withheld_conflict"][0]
        self.assertIn("conflicting duplicate observations", withheld["reasons"][0])


class AuditCliIntegrityTests(unittest.TestCase):
    """Workstream 5: the scorer and evaluator CLI entry points, not only the library functions."""

    ORG_A = "923609016"
    ORG_B = "987654321"

    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp())

    def write(self, name, rows):
        path = self.workdir / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return str(path)

    def observation(self, org, observation_id, **changes):
        import hashlib as _hashlib
        base = {
            "id": observation_id, "organisation_number": org, "platform": "google_places",
            "signal_type": "place_summary", "source_url": f"https://maps.example.invalid/place/{observation_id}",
            "retrieved_at": "2026-08-20T00:00:00Z",
            "content_sha256": _hashlib.sha256(observation_id.encode()).hexdigest(),
            "exact_entity": True, "identity_proof": [{"type": "synthetic_identity_proof"}],
            "acquisition_mode": "official_api", "rights_status": "approved",
            "source_class": "public_business_listing",
        }
        return {**base, **changes}

    def label(self, org, observation_id, **changes):
        return {"organisation_number": org, "id": observation_id, "exact_entity": True, "metric_correct": True, **changes}

    def invoke(self, command):
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180)

    def evaluator(self, observations, labels, *, minimum):
        profiles = self.write("profiles.jsonl", [{"organisation_number": self.ORG_A, "name": "A", "evidence": {}}])
        output = self.workdir / f"eval-{minimum}-{Path(observations).stem}.json"
        output.unlink(missing_ok=True)
        done = self.invoke([sys.executable, str(ROOT / "scripts" / "evaluate_external_footprint.py"),
                         "--profiles", profiles, "--observations", observations, "--labels", labels,
                         "--output", str(output), "--minimum-audit", str(minimum), "--as-of", "2026-08-24T00:00:00Z"])
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        return json.loads(output.read_text(encoding="utf-8"))

    def test_evaluator_cli_duplicates_cannot_close_the_audit_gate(self):
        observations = self.write("dup100.jsonl", [self.observation(self.ORG_A, "dup") for _ in range(100)])
        labels = self.write("dup100-labels.jsonl", [self.label(self.ORG_A, "dup")])
        report = self.evaluator(observations, labels, minimum=100)
        self.assertEqual(report["published_audited"], 1)
        self.assertFalse(report["audit_size_gate"])
        self.assertFalse(report["qualification_passed"])
        self.assertEqual(report["audit_eligibility"]["collapsed_identical"], 99)

    def test_evaluator_cli_out_of_batch_cannot_close_the_audit_gate(self):
        observations = self.write("outside100.jsonl", [self.observation(self.ORG_B, f"b-{index:03d}") for index in range(100)])
        labels = self.write("outside100-labels.jsonl", [self.label(self.ORG_B, f"b-{index:03d}") for index in range(100)])
        report = self.evaluator(observations, labels, minimum=100)
        self.assertEqual(report["published_audited"], 0)
        self.assertFalse(report["qualification_passed"])
        self.assertEqual(report["observations_out_of_batch"], 100)

    def test_evaluator_cli_distinct_in_batch_records_still_qualify(self):
        records = [self.observation(self.ORG_A, f"d-{index:03d}") for index in range(100)]
        observations = self.write("distinct100.jsonl", records)
        labels = self.write("distinct100-labels.jsonl", [self.label(self.ORG_A, row["id"]) for row in records])
        report = self.evaluator(observations, labels, minimum=100)
        self.assertEqual(report["published_audited"], 100)
        self.assertTrue(report["audit_size_gate"])
        self.assertTrue(report["qualification_passed"])

    def scorer(self, observations, labels, external_report):
        profiles = self.write("score-profiles.jsonl", [{
            "organisation_number": self.ORG_A, "name": "A",
            "evidence": {"registry_live": {"value": {"organisation_number": self.ORG_A}}},
        }])
        batch_report = self.workdir / "batch.json"
        batch_report.write_text(json.dumps({"validation": {"passed": True}, "emitted_envelopes": 1,
                                            "operations": {"p95_ms": 100}}), encoding="utf-8")
        refresh_report = self.workdir / "refresh.json"
        refresh_report.write_text(json.dumps({"qualification_passed": True, "evidence_complete": True,
                                              "idempotent_rerun": True}), encoding="utf-8")
        empty = self.workdir / "empty.json"
        empty.write_text("{}", encoding="utf-8")
        output = self.workdir / f"score-{Path(external_report).stem}.json"
        output.unlink(missing_ok=True)
        done = self.invoke([sys.executable, str(ROOT / "scripts" / "score_competition_v3.py"),
                         "--profiles", profiles, "--external-report", external_report,
                         "--batch-report", str(batch_report), "--refresh-report", str(refresh_report),
                         "--research-report", str(empty), "--ux-report", str(empty),
                         "--observations", observations, "--labels", labels,
                         "--as-of", "2026-08-24T00:00:00Z", "--output", str(output)])
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        return json.loads(output.read_text(encoding="utf-8"))

    def test_scorer_cli_derives_from_artifacts_and_rejects_a_fabricated_audit_claim(self):
        observations = self.write("score-dup100.jsonl", [self.observation(self.ORG_A, "dup") for _ in range(100)])
        labels = self.write("score-dup100-labels.jsonl", [self.label(self.ORG_A, "dup")])
        fabricated = self.workdir / "external-fabricated.json"
        fabricated.write_text(json.dumps({
            "published_audited": 100, "audit_size": 100, "audit_size_gate": True, "qualification_passed": True,
            "fresh_coverage": 1.0, "connector_policy_passed": True,
            "coverage": {"two_platforms": 1.0, "workforce_jobs": 1.0, "ratings_reviews": 1.0,
                         "buzz_engagement": 1.0, "sentiment": 1.0},
            "wrong_entity_publications": 0, "unsupported_publications": 0,
        }), encoding="utf-8")
        report = self.scorer(observations, labels, str(fabricated))
        mismatches = {entry["field"]: entry for entry in report["external_report_mismatches"]}
        self.assertIn("published_audited", mismatches)
        self.assertEqual(mismatches["published_audited"]["derived"], 1)
        self.assertFalse(report["qualification_gates"]["external_audit_at_least_100"])
        self.assertFalse(report["qualification_gates"]["external_report_consistent"])
        self.assertEqual(report["awardable_score"], 0)

    def test_scorer_cli_honest_report_agrees_with_the_derivation(self):
        records = [self.observation(self.ORG_A, f"s-{index:03d}") for index in range(100)]
        observations = self.write("score-distinct100.jsonl", records)
        labels = self.write("score-distinct100-labels.jsonl", [self.label(self.ORG_A, row["id"]) for row in records])
        honest = self.evaluator(observations, labels, minimum=100)
        honest_path = self.workdir / "external-honest.json"
        honest_path.write_text(json.dumps(honest), encoding="utf-8")
        report = self.scorer(observations, labels, str(honest_path))
        self.assertEqual(report["external_report_mismatches"], [])
        self.assertTrue(report["qualification_gates"]["external_audit_at_least_100"])
        self.assertEqual(report["measurement_source"], "derived_from_artifacts")


if __name__ == "__main__":
    unittest.main()
