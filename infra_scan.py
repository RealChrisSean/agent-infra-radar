"""Public ATS scanning and conservative, explainable job qualification.

No credentials, persistence, notifications, or applications live in this module.
Each result is a match to saved search criteria, not a claim of applicant fitness.
"""
import concurrent.futures
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = json.loads((ROOT / "infra_sources.json").read_text())
SOFTWARE = re.compile(r"\b(?:software|backend|back.end|full.stack|frontend|front.end|platform|infrastructure|systems|runtime|inference|machine learning|ml|research|security|cloud|performance|founding|product)\s+(?:software\s+)?engineer\b|\bengineer[ ,:–-]+(?:software|backend|platform|infrastructure|systems|inference|runtime)\b|\bmember of technical staff\b|\b(sre|developer)\b", re.I)
EXCLUDED_TITLE = re.compile(r"\b(?:manager|director|head|vp|vice president|intern|internship|recruiter|sales|solutions|solution|customer|support|advocate|advocacy|devrel|marketing|designer|design|data scientist|analyst|product manager|research scientist|it systems|documentation|technical writer|education|educator)\b|developer relations", re.I)
AGENT = re.compile(r"\b(?:agents?|agentic|langgraph|langsmith|langchain|claude code|tool (?:use|execution|calling)|function calling|sandboxes?|microvms?|firecracker|code execution|computer use|model serving|inference|llms?|large language models?|ai infrastructure|ai workloads?|model runtimes?)\b", re.I)
INFRA = re.compile(r"\b(?:infrastructure|runtime|platform|distributed|sandboxes?|microvms?|firecracker|orchestrat\w*|observability|evaluation|inference|serving|sdk|apis?|backend|deployment|systems|execution|compute|storage|kubernetes|containers?|framework)\b", re.I)
INFRA_TITLE = re.compile(r"\b(?:infrastructure|platform|systems|runtime|inference|storage|distributed|compute|backend|back.end|site reliability|sre|performance)\b", re.I)
DIRECT_AGENT = re.compile(r"\b(?:agent(?:ic)?\s+(?:runtime|infrastructure|platform|execution|orchestration|framework|observability)|(?:runtime|infrastructure|platform|execution|orchestration|framework)\s+(?:for\s+)?(?:ai\s+)?agents?|sandboxes?|microvms?|firecracker|code execution|computer use|tool (?:use|execution|calling)|function calling)\b", re.I)
MODEL_INFRA = re.compile(r"\b(?:model serving|inference (?:infrastructure|systems?|engines?|runtimes?|platform|serving|deployment|stack|compute|kernels?|optimization|performance|throughput|reliability|latency)|(?:distributed|low.latency|high.throughput) inference|llm (?:infrastructure|serving|runtime|gateway|observability|evaluation)|serving (?:models|llms))\b", re.I)
HARDWARE_IDENTITY = re.compile(r"\b(?:manufacturing|silicon|firmware|embedded|robotics|consumer devices|first.party hardware)\b", re.I)
RESEARCH_IDENTITY = re.compile(r"\b(?:research|training|reinforcement learning|interpretability)\b", re.I)
CORE_EXECUTION = re.compile(r"\b(?:agent(?:ic)?\s+(?:runtime|infrastructure|execution|orchestration)|(?:runtime|infrastructure|execution|orchestration)\s+(?:for\s+)?(?:ai\s+)?agents?|sandboxes?|microvms?|firecracker|code execution)\b", re.I)
MODEL_INFRA_TITLE = re.compile(r"\b(?:inference|model)\s+(?:runtime|systems?|infrastructure|platform|deployment|serving|engine)\b", re.I)
ROLE_MARKER = re.compile(r"(?im)^\s*(?:#+\s*)?(?:about (?:the|this) (?:role|team)|the role|your role|what you(?:'|’)ll do|what you will do|responsibilities|key responsibilities|what we're looking for|what we’re looking for)\s*[:\n]?")
SKILLS = {"python", "typescript", "javascript", "golang", "go", "rust", "c++", "java", "kubernetes", "docker", "terraform", "aws", "gcp", "azure", "linux", "postgres", "postgresql", "redis", "sqlite", "react", "next.js", "fastapi", "django", "flask", "node.js", "pytorch", "tensorflow", "cuda", "grpc", "graphql", "opentelemetry", "langchain", "langgraph", "firecracker", "ebpf", "nomad"}
MANAGEMENT = re.compile(r"(?:you(?:'ll| will)|responsibilit\w*|role)[^.\n]{0,100}(?:people manag|performance review|manage (?:a |the )?team)|\b(?:direct reports|people management responsibilities|hiring and performance management)\b", re.I)
FOREIGN = re.compile(r"\b(?:canada|canadian|toronto|vancouver|montreal|europe|emea|apac|london|united kingdom|\buk\b|germany|berlin|prague|czech|india|bengaluru|singapore|australia|sydney|brazil|latam|mexico|ireland|dublin|stockholm|sweden|paris|france|zurich|switzerland)\b", re.I)
US = re.compile(r"\b(?:united states|usa|u\.s\.?|us|nevada|nv)\b", re.I)
MONEY_RANGE = re.compile(r"(?P<cur>US\$|CA\$|C\$|A\$|USD\s*\$?|EUR\s*|GBP\s*|CAD\s*|AUD\s*|[$€£])\s*(?P<lo>\d[\d,]*(?:\.\d+)?)\s*(?P<lk>[kK])?\s*(?:-|–|—|to)\s*(?:(?:US\$|CA\$|C\$|A\$|USD\s*\$?|EUR\s*|GBP\s*|CAD\s*|AUD\s*|[$€£])\s*)?(?P<hi>\d[\d,]*(?:\.\d+)?)\s*(?P<hk>[kK])?", re.I)


def now():
    return datetime.now(timezone.utc).isoformat()


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in ("p", "div", "li", "br", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag in ("p", "div", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain(value):
    parser = PlainText()
    parser.feed(html.unescape(str(value or "")))
    return re.sub(r"[ \t]+", " ", "".join(parser.parts)).strip()


def role_scope(description):
    """Prefer actual role/team text over an employer's generic AI introduction."""
    marker = re.search(r"(?im)^\s*(?:#+\s*)?(?:about (?:the|this) role|the role|your role)\s*[:\n]?", description) or ROLE_MARKER.search(description)
    if marker:
        description = description[marker.start():]
    end = re.search(r"(?im)^\s*(?:#+\s*)?(?:you (?:might thrive|may be a good fit)|what you(?:'|’)ll bring|what we(?:'|’)re looking for|requirements|minimum qualifications|preferred qualifications|nice to have|about openai|about anthropic|benefits|logistics|how we(?:'|’)re different)\s*[:\n]?", description)
    return description[:end.start()] if end and end.start() > 0 else description


def keyword_matches(keywords, text):
    return [str(keyword) for keyword in keywords if str(keyword).strip() and re.search(r"(?<![\w])" + re.escape(str(keyword).strip()) + r"(?![\w])", text, re.I)]


def infrastructure_evidence(title, description):
    scope = role_scope(description)
    combined = title + "\n" + scope
    core = CORE_EXECUTION.search(combined)
    if HARDWARE_IDENTITY.search(title) and not core:
        return False, "Hardware/manufacturing role; direct agent runtime infrastructure duties need confirmation."
    if RESEARCH_IDENTITY.search(title) and not core:
        return False, "Research/training role; direct agent runtime infrastructure duties need confirmation."
    if re.search(r"\b(?:gtm|go.to.market)\b", title, re.I):
        return False, "Internal go-to-market systems role; agent infrastructure product scope needs confirmation."
    if re.search(r"\bfront.?end\b", title, re.I) and not core:
        return False, "Frontend role; direct agent runtime infrastructure duties need confirmation."
    model_title = MODEL_INFRA_TITLE.search(title)
    if model_title:
        return True, "Role title explicitly targets " + model_title.group(0) + "."
    direct = DIRECT_AGENT.search(title)
    if direct:
        return True, "Role title explicitly targets " + direct.group(0) + "."
    # Infrastructure roles need concrete duties beyond a company saying it uses AI.
    concrete = MODEL_INFRA.search(scope) or DIRECT_AGENT.search(scope)
    if INFRA_TITLE.search(title) and concrete:
        return True, "Infrastructure role with " + concrete.group(0) + " evidence in the role description."
    # Generic SWE/product/frontend titles can qualify only when the work itself
    # explicitly builds an agent or model-infrastructure component.
    for sentence in re.split(r"[.!?\n]", scope):
        duty = re.search(r"(?:^\s*(?:[-*•]\s*)?(?:build|design|develop|implement|own|scale|operate|maintain|ship)|\byou(?:'ll|’ll| will)|\byour (?:work|job|role)|\bwork (?:across|on))", sentence, re.I)
        specific = DIRECT_AGENT.search(sentence) or MODEL_INFRA.search(sentence)
        if duty and specific:
            return True, "Role duties explicitly include " + specific.group(0) + "."
        if duty and AGENT.search(sentence) and INFRA.search(sentence):
            return True, "Role duties build agent infrastructure: " + sentence.strip()[:200] + "."
    if AGENT.search(combined):
        return False, "Agent/AI context is present, but direct infrastructure duties need confirmation."
    return False, "No agent, sandbox, or LLM/inference infrastructure evidence in the role description."


def canonical_url(value):
    p = urllib.parse.urlsplit(str(value or ""))
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("Job URL must be public HTTPS")
    return urllib.parse.urlunsplit(("https", p.netloc.lower(), p.path.rstrip("/"), "", ""))


def source_url(source):
    provider, slug = source.get("provider"), source.get("slug", "")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", slug):
        raise ValueError("Invalid public ATS board name")
    if provider == "ashby":
        return "https://api.ashbyhq.com/posting-api/job-board/" + slug + "?includeCompensation=true"
    if provider == "greenhouse":
        return "https://boards-api.greenhouse.io/v1/boards/" + slug + "/jobs?content=true"
    if provider == "lever":
        return "https://api.lever.co/v0/postings/" + slug + "?mode=json"
    raise ValueError("Unsupported ATS provider")


def board_url(source):
    host = {"ashby": "https://jobs.ashbyhq.com/", "greenhouse": "https://job-boards.greenhouse.io/", "lever": "https://jobs.lever.co/"}[source["provider"]]
    return host + source["slug"]


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "AgentInfraRadar/1.0 (public careers monitor)", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.headers.get_content_type() not in ("application/json", "text/json"):
            raise ValueError("ATS returned a non-JSON response")
        raw = response.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("ATS response exceeded size limit")
        return json.loads(raw)


def _amount(value):
    try:
        n = float(value)
        return n if 0 < n < 5000000 else None
    except (TypeError, ValueError):
        return None


def extract_salary(raw, description="", location=""):
    """Only annual salary/base-pay evidence can clear the notification threshold."""
    result = dict(salary_min=None, salary_max=None, salary_currency=None, salary_interval=None,
                  salary_verified=False, salary_evidence="", salary_ambiguous=False)
    comp = raw.get("compensation") or {}
    if isinstance(comp, dict):
        components = comp.get("summaryComponents") or []
        if not components:
            components = [c for tier in comp.get("compensationTiers", []) if isinstance(tier, dict) for c in tier.get("components", [])]
        salary = [c for c in components if isinstance(c, dict) and str(c.get("compensationType", "")).lower() in ("salary", "base", "base salary")]
        if salary:
            currencies = {str(c.get("currencyCode", "")).upper() for c in salary}
            annual = all(str(c.get("interval", "")).lower() in ("1 year", "year", "annual", "yearly") for c in salary)
            lows = [_amount(c.get("minValue")) for c in salary]
            highs = [_amount(c.get("maxValue")) for c in salary]
            result.update(salary_min=min(lows) if all(lows) else None, salary_max=max(highs) if all(highs) else None,
                          salary_currency=next(iter(currencies)) if len(currencies) == 1 else None,
                          salary_interval="year" if annual else str(salary[0].get("interval", "")).lower(),
                          salary_evidence="; ".join(str(c.get("summary") or "Structured employer salary component") for c in salary),
                          salary_ambiguous=len(currencies) != 1)
            result["salary_verified"] = annual and len(currencies) == 1 and bool(result["salary_min"] and result["salary_max"])
            return result
    lever = raw.get("salaryRange")
    if isinstance(lever, dict) and lever:
        result.update(salary_min=_amount(lever.get("min")), salary_max=_amount(lever.get("max")),
                      salary_currency=str(lever.get("currency", "")).upper() or None,
                      salary_interval=str(lever.get("interval", "")).lower(), salary_evidence="Employer salaryRange field")
        if result["salary_interval"] in ("per-year-salary", "yearly", "annual", "year"):
            result["salary_interval"] = "year"
        result["salary_verified"] = result["salary_interval"] == "year" and bool(result["salary_min"] and result["salary_max"])
        return result
    text = plain(description)
    candidates = []
    for match in MONEY_RANGE.finditer(text):
        # A nearby salary label is required. Equity, stipend, OTE, and hourly
        # amounts are never annualized or treated as a guaranteed base.
        before = text[max(0, match.start()-160):match.start()]
        after = text[match.end():match.end()+70]
        context = before + match.group() + after
        nearest_label = re.split(r"[.!?\n]", before)[-1]
        if not re.search(r"(?:salary|base pay|base compensation|annual pay)", before[-130:], re.I):
            continue
        if re.search(r"(?:total compensation|on.target earnings|\bOTE\b|including (?:equity|bonus)|stipend)", nearest_label, re.I):
            continue
        if re.search(r"(?:per hour|hourly|/\s*h(?:ou)?r|per month|monthly|/\s*month)", context, re.I):
            continue
        cur = match.group("cur").upper()
        currency = "EUR" if "€" in cur or "EUR" in cur else "GBP" if "£" in cur or "GBP" in cur else "CAD" if cur in ("CA$", "C$", "CAD") else "AUD" if cur in ("A$", "AUD") else "USD"
        suffix = after[:15].upper()
        if re.search(r"\b(?:CAD|AUD|EUR|GBP)\b", suffix):
            currency = re.search(r"\b(CAD|AUD|EUR|GBP)\b", suffix).group(1)
        # A bare $ is not enough to determine currency on a foreign posting.
        if currency == "USD" and cur == "$" and not re.search(r"\bUSD\b", context) and FOREIGN.search(location) and not US.search(location) and "san francisco" not in location.lower():
            continue
        lo = float(match.group("lo").replace(",", "")) * (1000 if match.group("lk") else 1)
        hi = float(match.group("hi").replace(",", "")) * (1000 if match.group("hk") else 1)
        if match.group("hk") and not match.group("lk") and lo < 1000:
            lo *= 1000
        if not 10000 <= lo <= hi < 5000000:
            continue
        candidates.append((lo, hi, currency, context.strip()))
    if candidates:
        currencies = {c[2] for c in candidates}
        result.update(salary_min=min(c[0] for c in candidates), salary_max=max(c[1] for c in candidates),
                      salary_currency=next(iter(currencies)) if len(currencies) == 1 else None,
                      salary_interval="year", salary_verified=len(currencies) == 1,
                      salary_ambiguous=len(currencies) != 1,
                      salary_evidence="; ".join(c[3] for c in candidates)[:1000])
    return result


def location_assessment(job, settings):
    location = str(job.get("location") or "")
    description = str(job.get("description") or "")
    location_lower = location.lower()
    selected = settings.get("locations", ["San Francisco"])
    if isinstance(selected, str):
        selected = [s.strip() for s in selected.split(",") if s.strip()]
    # Honor an explicit office/remote restriction before treating a remote badge
    # as national eligibility. A selected office still makes relocation possible.
    for city in selected:
        if city and str(city).lower() in location_lower:
            return "match", "Employer lists " + str(city) + " as a work location."
        if str(city).lower() == "san francisco" and re.search(r"\bSF\b", location):
            return "match", "Employer lists San Francisco as a work location."
    remote = job.get("remote") is True or "remote" in location_lower or str(job.get("workplace_type", "")).lower() == "remote"
    if remote and settings.get("remote_allowed", True):
        local = re.search(r"(?:must|need to|required to)\s+(?:be |reside |live |work )*(?:located |based )?(?:in|within)\s+([^.!\n]{1,120})", description, re.I)
        if local and not US.search(local.group(1)) and not re.search(r"anywhere|worldwide|globally", local.group(1), re.I):
            return "review", "Remote role has a location restriction: " + local.group(0)[:150]
        if re.search(r"(?:excluding|except|not (?:available|eligible|hiring) in)[^.!\n]{0,80}\b(?:Nevada|NV)\b", description, re.I):
            return "excluded", "Remote policy explicitly excludes Nevada."
        region = str(settings.get("remote_region", "United States"))
        if region.lower() not in ("united states", "us", "usa", "nevada"):
            if region.lower() in location_lower:
                return "match", "Remote location matches " + region + "."
            return "review", "Remote eligibility for " + region + " is not confirmed."
        if FOREIGN.search(location) and not US.search(location):
            return "excluded", "Remote role is listed outside the United States."
        if re.search(r"(?:hybrid|\b[1-5]\s+days? (?:per |a )?week (?:in|at) (?:the |our )?office|in.office (?:at least|minimum)|office.{0,30}at least.{0,10}%)", description, re.I):
            return "review", "Remote label also includes office attendance; eligible location needs confirmation."
        state_restriction = re.search(r"(?:eligible (?:states|locations)|(?:only|must be based) in)[^.!\n]{0,180}", description, re.I)
        if state_restriction and not re.search(r"\b(?:Nevada|NV|all (?:50 )?states|anywhere (?:in )?(?:the )?(?:US|United States))\b", state_restriction.group(0), re.I):
            return "review", "Remote work has an eligibility restriction requiring review: " + state_restriction.group(0)[:150]
        if US.search(location) or re.search(r"\b(?:worldwide|anywhere|global(?:ly)?)\b", location, re.I):
            return "match", "Employer explicitly lists US or worldwide remote work."
        if re.search(r"(?:remote.{0,40}(?:united states|nevada)|(?:united states|nevada).{0,40}remote)", description, re.I):
            return "match", "Description explicitly allows US remote work."
        return "review", "Remote work is listed, but US/Nevada eligibility is not explicit."
    if remote:
        return "excluded", "Remote work is disabled in settings."
    if not location or re.search(r"bay area|foster city|palo alto|san mateo|redwood city|mountain view|san jose|oakland|berkeley|sunnyvale", location, re.I):
        return "review", "Exact work location is outside the selected city or needs confirmation."
    return "excluded", "Onsite/hybrid location does not match settings."


def categorize_job(job, settings=None):
    settings = settings or {}
    job = dict(job)
    title, description = str(job.get("title", "")), str(job.get("description", ""))
    evidence = title + "\n" + role_scope(description)
    reasons, caveats, excluded = [], [], []
    if EXCLUDED_TITLE.search(title):
        excluded.append("Title is outside individual-contributor software engineering.")
    elif not SOFTWARE.search(title):
        caveats.append("Software engineering responsibilities need manual confirmation.")
    if MANAGEMENT.search(description):
        excluded.append("Posting includes people-management responsibilities.")
    if str(job.get("employment_type", "")).lower().replace(" ", "") in ("intern", "internship", "parttime", "temporary", "contract"):
        excluded.append("Role is not a full-time employee position.")
    is_infra, infra_reason = infrastructure_evidence(title, description)
    if is_infra:
        reasons.append(infra_reason)
    elif not AGENT.search(title + "\n" + description):
        excluded.append(infra_reason)
    else:
        caveats.append(infra_reason)
    includes = settings.get("include_keywords", [])
    excluded_keywords = keyword_matches(settings.get("exclude_keywords", []), title)
    included_keywords = keyword_matches(includes, evidence)
    if excluded_keywords:
        excluded.append("Title matches excluded keywords: " + ", ".join(excluded_keywords) + ".")
    if includes and not included_keywords:
        excluded.append("No saved include keyword appears in the role title or duties.")
    elif included_keywords:
        reasons.append("Saved keyword matches: " + ", ".join(included_keywords) + ".")
    shared_skills = sorted(skill for skill in SKILLS if keyword_matches([skill], str(settings.get("profile_text", ""))) and keyword_matches([skill], evidence))
    if shared_skills:
        reasons.append("Shared skill terms with your profile (not a qualification assessment): " + ", ".join(shared_skills) + ".")
    loc_status, loc_reason = location_assessment(job, settings)
    (reasons if loc_status == "match" else excluded if loc_status == "excluded" else caveats).append(loc_reason)
    wanted_currency = settings.get("currency", "USD")
    if not job.get("salary_verified") or job.get("salary_interval") != "year" or job.get("salary_ambiguous"):
        caveats.append("Annual base salary is not verified; no salary-based alert yet.")
    elif job.get("salary_currency") != wanted_currency:
        excluded.append("Published salary currency does not match " + wanted_currency + ".")
    else:
        floor = settings.get("min_salary", 150000)
        use_maximum = settings.get("salary_rule", "minimum") in ("range_reaches", "maximum", "range_maximum", "overlap")
        value = job.get("salary_max") if use_maximum else job.get("salary_min")
        if value is None or value < floor:
            excluded.append("Published salary " + ("maximum" if use_maximum else "minimum") + " is below the saved threshold.")
        else:
            reasons.append("Published annual " + wanted_currency + " base salary clears the saved threshold.")
    if job.get("status", "open") != "open":
        excluded.append("Posting is not open.")
    job.update(match_status="excluded" if excluded else "review" if caveats else "match", reasons=reasons + excluded, caveats=caveats)
    return job


classify_job = categorize_job


def normalize(raw, source, checked_at):
    provider = source["provider"]
    categories = raw.get("categories") or {}
    title = str(raw.get("title") or raw.get("text") or "").strip()
    location = raw.get("location") or categories.get("location") or ""
    if isinstance(location, dict):
        location = location.get("name", "")
    secondary = raw.get("secondaryLocations") or []
    location = "; ".join(dict.fromkeys([str(location)] + [str(x.get("location", "")) for x in secondary if isinstance(x, dict)]))
    body = raw.get("descriptionPlain") or raw.get("descriptionHtml") or raw.get("content") or raw.get("description") or ""
    if provider == "lever":
        body = str(body) + "\n" + "\n".join(str(x.get("text", "")) + "\n" + str(x.get("content", "")) for x in raw.get("lists", [])) + "\n" + str(raw.get("additionalPlain") or raw.get("additional") or "")
    description = plain(body)
    url = canonical_url(raw.get("jobUrl") or raw.get("absolute_url") or raw.get("hostedUrl") or raw.get("url"))
    job = dict(id=hashlib.sha256(url.encode()).hexdigest()[:20], company=source["company"], title=title, url=url,
               location=location, remote=bool(raw.get("isRemote") or str(raw.get("workplaceType", "")).lower() == "remote" or "remote" in location.lower()),
               workplace_type=raw.get("workplaceType"), employment_type=raw.get("employmentType") or categories.get("commitment"),
               description=description, source=provider, source_url=board_url(source), verified_at=checked_at,
               first_seen=checked_at, last_seen=checked_at, status="open")
    job.update(extract_salary(raw, description, location))
    return job


def scan_source(source, settings, fetcher=fetch_json):
    checked = now()
    coverage = dict(company=source.get("company", "Unknown"), provider=source.get("provider"), checked_at=checked, count=0, status="failed")
    jobs = []
    try:
        url = source_url(source)
        coverage["url"] = url
        data = fetcher(url)
        if isinstance(data, (bytes, str)):
            data = json.loads(data)
        provider = source["provider"]
        rows = data if provider == "lever" else data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("ATS response does not contain a jobs list")
        if any(not isinstance(r, dict) or not (r.get("title") or r.get("text")) for r in rows):
            raise ValueError("ATS jobs list is malformed")
        if provider == "lever" and len(rows) >= 100:
            # Lever can cap a feed. Page explicitly and reject repeated pages.
            seen = {str(r.get("id") or r.get("hostedUrl")) for r in rows}
            skip = len(rows)
            while len(rows) < 10000:
                page = fetcher(url + "&skip=" + str(skip) + "&limit=100")
                if not isinstance(page, list):
                    raise ValueError("Lever pagination returned invalid data")
                if not page:
                    break
                fresh = [r for r in page if isinstance(r, dict) and str(r.get("id") or r.get("hostedUrl")) not in seen]
                if not fresh:
                    coverage.update(status="partial", error="Lever pagination repeated an earlier page")
                    break
                rows.extend(fresh)
                seen.update(str(r.get("id") or r.get("hostedUrl")) for r in fresh)
                skip += len(page)
                if len(page) < 100:
                    break
            else:
                coverage.update(status="partial", error="Lever pagination safety limit reached")
        coverage["count"] = len(rows)
        if coverage["status"] != "partial":
            coverage["status"] = "completed"
        if isinstance(data, dict) and (data.get("hasMore") or data.get("nextCursor") or (data.get("meta", {}).get("total", 0) > len(rows))):
            coverage.update(status="partial", error="ATS indicates additional unreviewed results")
        invalid = 0
        for raw in rows:
            if raw.get("isListed") is False:
                continue
            # Include custom keyword discoveries in candidate collection too.
            # They still pass through non-SWE/management and relevance checks.
            title = raw.get("title") or raw.get("text") or ""
            raw_text = str(raw.get("descriptionPlain") or raw.get("descriptionHtml") or raw.get("content") or raw.get("description") or "")
            custom_match = keyword_matches(settings.get("include_keywords", []), title + "\n" + plain(raw_text))
            if not SOFTWARE.search(title) and not custom_match:
                continue
            try:
                job = categorize_job(normalize(raw, source, checked), settings)
                if AGENT.search(job["title"] + "\n" + job["description"]) or custom_match:
                    jobs.append(job)
            except (TypeError, ValueError, KeyError):
                invalid += 1
        if invalid:
            coverage.update(status="partial", error=str(invalid) + " engineering postings could not be normalized")
    except Exception as error:
        # Remote response bodies are not logged; only the error class/status.
        message = "HTTP " + str(error.code) if isinstance(error, urllib.error.HTTPError) else str(error)
        coverage.update(status="failed", error=(type(error).__name__ + ": " + message)[:240])
    return jobs, coverage


def scan(settings=None, fetcher=fetch_json):
    settings = settings or {}
    sources = settings.get("sources", DEFAULT_SOURCES)
    sources = [s for s in sources if isinstance(s, dict) and s.get("enabled", True)]
    # Bound concurrency while the app remains responsive during public requests.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(sources)))) as pool:
        results = list(pool.map(lambda s: scan_source(s, settings, fetcher), sources))
    jobs = {j["id"]: j for found, _ in results for j in found}
    coverage = [c for _, c in results]
    completed = sum(c["status"] == "completed" for c in coverage)
    failed = sum(c["status"] == "failed" for c in coverage)
    status = "failed" if not coverage or failed == len(coverage) else "completed" if completed == len(coverage) else "partial"
    matches = sum(j["match_status"] == "match" for j in jobs.values())
    summary = f"{matches} matching roles; {completed}/{len(coverage)} public employer feeds completed."
    if status != "completed":
        summary += " Incomplete feeds do not establish that no jobs exist."
    return dict(jobs=list(jobs.values()), coverage=coverage, status=status, summary=summary)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path)
    args = parser.parse_args()
    print(json.dumps(scan(json.loads(args.settings.read_text()) if args.settings else {}), indent=2))
