import unittest
from infra_scan import categorize_job, extract_salary, location_assessment, normalize, scan


def salary_component(lo=180000, hi=240000, currency="USD", interval="1 YEAR", kind="Salary"):
    return {"compensationType": kind, "interval": interval, "currencyCode": currency, "minValue": lo, "maxValue": hi}


def job(**changes):
    j = dict(title="Software Engineer, Agent Runtime", company="Example", location="San Francisco", remote=False,
             description="Build distributed infrastructure for agent execution and sandbox orchestration.",
             salary_min=180000, salary_max=240000, salary_currency="USD", salary_interval="year",
             salary_verified=True, status="open")
    j.update(changes)
    return j


class SalaryTests(unittest.TestCase):
    def test_structured_salary_excludes_equity_and_bonus(self):
        data = {"compensation": {"summaryComponents": [salary_component(), salary_component(50000, 100000, kind="Bonus"), salary_component(.1, .5, kind="EquityPercentage")]}}
        result = extract_salary(data)
        self.assertTrue(result["salary_verified"])
        self.assertEqual((result["salary_min"], result["salary_max"]), (180000, 240000))

    def test_salary_tiers_use_conservative_floor(self):
        result = extract_salary({"compensation": {"compensationTiers": [{"components": [salary_component(140000, 200000)]}, {"components": [salary_component(180000, 250000)]}]}})
        self.assertEqual(result["salary_min"], 140000)
        self.assertTrue(result["salary_verified"])

    def test_nonannual_and_nonusd_never_clear_default(self):
        for interval, currency in [("1 HOUR", "USD"), ("1 YEAR", "EUR")]:
            result = extract_salary({"compensation": {"summaryComponents": [salary_component(100, 200, currency, interval)]}})
            self.assertNotEqual(categorize_job(job(**result))["match_status"], "match")

    def test_mixed_currency_tiers_need_review(self):
        result = extract_salary({"compensation": {"summaryComponents": [salary_component(), salary_component(currency="CAD")]}})
        self.assertFalse(result["salary_verified"])
        self.assertTrue(result["salary_ambiguous"])

    def test_annual_html_salary_and_k_suffix(self):
        for text in ["<p>Annual Salary:</p><p>$175,000 - $240,000 USD</p>", "The base salary range for this role is $175K – $240K.", "Salary range: $175-240K USD"]:
            result = extract_salary({}, text, "San Francisco")
            self.assertEqual(result["salary_min"], 175000)
            self.assertEqual(result["salary_max"], 240000)
            self.assertTrue(result["salary_verified"])

    def test_hourly_total_compensation_and_equity_not_base(self):
        for text in ["Base salary is $150-$200 per hour", "Total compensation ranges from $200K - $350K annually", "OTE salary range $200K - $350K", "Equity range $150K - $200K", "Signing bonus: $150K - $200K", "Annual salary including equity: $150K - $200K"]:
            self.assertFalse(extract_salary({}, text, "San Francisco")["salary_verified"], text)

    def test_foreign_dollars_and_lever_hourly(self):
        result = extract_salary({}, "Annual salary $175,000-$240,000 CAD", "Canada")
        self.assertEqual(result["salary_currency"], "CAD")
        self.assertFalse(extract_salary({}, "Base salary $175,000-$240,000", "Toronto")["salary_verified"])
        result = extract_salary({"salaryRange": {"min": 80, "max": 150, "currency": "USD", "interval": "per-hour-wage"}})
        self.assertFalse(result["salary_verified"])


class ClassificationTests(unittest.TestCase):
    def test_hard_salary_floor_and_editable_range_rule(self):
        candidate = job(salary_min=140000, salary_max=220000)
        self.assertEqual(categorize_job(candidate, {"min_salary": 150000})["match_status"], "excluded")
        self.assertEqual(categorize_job(candidate, {"min_salary": 150000, "salary_rule": "range_reaches"})["match_status"], "match")
        self.assertEqual(categorize_job(job(salary_min=150000), {})["match_status"], "match")

    def test_missing_salary_review(self):
        result = categorize_job(job(salary_min=None, salary_max=None, salary_verified=False))
        self.assertEqual(result["match_status"], "review")

    def test_reclassification_uses_new_settings(self):
        self.assertEqual(categorize_job(job(), {"min_salary": 250000})["match_status"], "excluded")
        self.assertEqual(categorize_job(job(), {"locations": ["New York"]})["match_status"], "excluded")

    def test_nonengineering_and_management_excluded(self):
        for title in ["Developer Advocate", "Developer Education Lead, Claude Platform", "Developer Advocacy Engineer", "Sales Engineer", "Engineering Manager", "Software Engineering Intern", "IT Systems Engineer", "Solutions Engineer", "Product Manager"]:
            self.assertEqual(categorize_job(job(title=title))["match_status"], "excluded", title)
        self.assertEqual(categorize_job(job(description="Build agent runtimes. You will manage a team and conduct performance reviews."))["match_status"], "excluded")

    def test_hardware_and_research_are_not_blanket_infra_matches(self):
        for title in ["Software Engineer, Manufacturing Infrastructure", "Systems Software Engineer, Silicon Bringup", "Research Engineer, Interpretability"]:
            candidate = job(title=title, description="About the Team\nOur AI workloads use model inference.\nAbout the Role\nBuild software for hardware qualification and training experiments.")
            self.assertNotEqual(categorize_job(candidate)["match_status"], "match", title)
        candidate = job(title="Research Engineer, Agent Runtime", description="Build sandbox infrastructure for agent execution.")
        self.assertEqual(categorize_job(candidate)["match_status"], "match")

    def test_exact_model_runtime_titles_stay_relevant(self):
        for title in ["Software Engineer, Model Runtime", "Performance Engineer, Inference Systems"]:
            self.assertEqual(categorize_job(job(title=title))["match_status"], "match")

    def test_qualifications_and_team_blurbs_do_not_establish_role_scope(self):
        candidate = job(title="Software Engineer, Manufacturing Infrastructure", description="About the Team\nWe build AI agent infrastructure.\nAbout the Role\nBuild factory qualification software.\nRequirements\nExperience with agent runtimes.")
        self.assertNotEqual(categorize_job(candidate)["match_status"], "match")

    def test_irrelevant_engineering_excluded(self):
        self.assertEqual(categorize_job(job(title="Backend Engineer", description="Build payment reconciliation APIs."))["match_status"], "excluded")

    def test_generic_company_ai_boilerplate_is_not_infra_evidence(self):
        candidate = job(title="Frontend Engineer", description="We make LLMs available to everyone.\nAbout the Role\nBuild website interfaces and frontend API clients.")
        self.assertEqual(categorize_job(candidate)["match_status"], "review")
        candidate["description"] = "We are the AI agent platform. Build interfaces and APIs for our marketing website."
        self.assertEqual(categorize_job(candidate)["match_status"], "review")

    def test_concrete_role_duties_can_establish_agent_infra(self):
        candidate = job(title="Software Engineer", description="About the Role\nBuild an execution sandbox for AI agents using Firecracker.")
        self.assertEqual(categorize_job(candidate)["match_status"], "match")
        candidate = job(title="Infrastructure Engineer", description="About the Team\nOwn model serving infrastructure and inference reliability.")
        self.assertEqual(categorize_job(candidate)["match_status"], "match")

    def test_include_exclude_settings_change_results(self):
        candidate = job(description="Build Rust infrastructure for AI agent execution.")
        self.assertEqual(categorize_job(candidate, {"include_keywords": ["rust"]})["match_status"], "match")
        self.assertEqual(categorize_job(candidate, {"include_keywords": ["python"]})["match_status"], "excluded")
        self.assertEqual(categorize_job(candidate, {"exclude_keywords": ["runtime"]})["match_status"], "excluded")
        candidate["description"] += " Work with the sales team."
        self.assertEqual(categorize_job(candidate, {"exclude_keywords": ["sales"]})["match_status"], "match")

    def test_profile_terms_are_literal_and_labelled(self):
        candidate = job(description="Build Rust and Python infrastructure for AI agents using Kubernetes.")
        result = categorize_job(candidate, {"profile_text": "Python and Kubernetes. Interested in gardening."})
        profile_reasons = [r for r in result["reasons"] if "Shared skill terms" in r]
        self.assertEqual(len(profile_reasons), 1)
        self.assertIn("kubernetes, python", profile_reasons[0])
        self.assertIn("not a qualification assessment", profile_reasons[0])
        self.assertNotIn("gardening", profile_reasons[0])

    def test_remote_geography(self):
        for location in ["Remote - United States", "US Remote", "Remote, Nevada", "Remote Worldwide"]:
            self.assertEqual(categorize_job(job(location=location, remote=True))["match_status"], "match", location)
        for location in ["Remote - Canada", "Remote UK", "Remote EMEA"]:
            self.assertEqual(categorize_job(job(location=location, remote=True))["match_status"], "excluded", location)
        for location in ["Remote", "North America - Remote"]:
            self.assertEqual(categorize_job(job(location=location, remote=True))["match_status"], "review", location)

    def test_nevada_exclusion_and_office_conflict(self):
        candidate = job(location="US Remote", remote=True, description="Build infrastructure for agents. Remote, excluding Nevada.")
        self.assertEqual(categorize_job(candidate)["match_status"], "excluded")
        candidate["description"] = "Build infrastructure for agents. We work 3 days a week in our office."
        self.assertEqual(categorize_job(candidate)["match_status"], "review")

    def test_exact_city_and_secondary_locations(self):
        self.assertEqual(categorize_job(job(location="Foster City, CA"))["match_status"], "review")
        self.assertEqual(categorize_job(job(location="San Francisco; New York"))["match_status"], "match")
        self.assertEqual(categorize_job(job(location="Palo Alto"), {"locations": ["Palo Alto"]})["match_status"], "match")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.source = {"provider": "ashby", "slug": "example", "company": "Example", "enabled": True}
        self.raw = {"id": "1", "title": "Software Engineer, Agent Runtime", "location": "San Francisco", "jobUrl": "https://jobs.ashbyhq.com/example/1", "descriptionHtml": "Build infrastructure for AI agents.", "compensation": {"summaryComponents": [salary_component()]}}

    def test_scan_returns_normalized_job_and_complete_scope(self):
        result = scan({"sources": [self.source]}, lambda url: {"jobs": [self.raw]})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["jobs"][0]["match_status"], "match")
        self.assertEqual(result["coverage"][0]["count"], 1)
        self.assertIn("includeCompensation=true", result["coverage"][0]["url"])

    def test_failed_feed_not_zero_success(self):
        def fail(url):
            raise TimeoutError("Feed timed out")
        result = scan({"sources": [self.source]}, fail)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["coverage"][0]["status"], "failed")
        self.assertIn("Incomplete", result["summary"])

    def test_pagination_flag_is_partial(self):
        result = scan({"sources": [self.source]}, lambda url: {"jobs": [self.raw], "hasMore": True})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"][0]["status"], "partial")

    def test_hidden_jobs_not_recommended_and_invalid_url_partial(self):
        hidden = dict(self.raw, isListed=False)
        result = scan({"sources": [self.source]}, lambda url: {"jobs": [hidden]})
        self.assertEqual(result["jobs"], [])
        invalid = dict(self.raw, jobUrl="javascript:alert(1)")
        result = scan({"sources": [self.source]}, lambda url: {"jobs": [invalid]})
        self.assertEqual(result["status"], "partial")

    def test_disabled_source_not_fetched(self):
        result = scan({"sources": [dict(self.source, enabled=False)]}, lambda url: self.fail("Must not fetch"))
        self.assertEqual(result["coverage"], [])
        self.assertEqual(result["status"], "failed")

    def test_secondary_location_retained(self):
        result = normalize(dict(self.raw, location="New York", secondaryLocations=[{"location": "San Francisco"}]), self.source, "2026-10-08T00:00:00Z")
        self.assertEqual(result["location"], "New York; San Francisco")

    def test_custom_keywords_participate_in_candidate_collection(self):
        candidate = dict(self.raw, title="AI Engineer", descriptionHtml="Build distributed inference for AI agents using Rust.")
        result = scan({"sources": [self.source], "include_keywords": ["Rust"]}, lambda url: {"jobs": [candidate]})
        self.assertEqual(len(result["jobs"]), 1)
        self.assertIn("Saved keyword matches: Rust.", result["jobs"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
