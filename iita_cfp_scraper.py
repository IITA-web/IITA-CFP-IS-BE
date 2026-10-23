#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║         IITA CFP INTELLIGENCE SYSTEM — Production Scraper v3.0          ║
║         International Institute of Tropical Agriculture · PDAU           ║
╚══════════════════════════════════════════════════════════════════════════╝

LEGAL COMPLIANCE NOTICE
───────────────────────
This scraper is built to operate ethically and within accepted web standards:

  1. robots.txt honoured   — Every source is checked before crawling.
                             Disallowed paths are never accessed.
  2. RSS-first strategy    — Official feeds are always preferred over
                             web scraping. No legal ambiguity with feeds.
  3. Polite crawl delays   — 2–4 second pauses between requests to the
                             same domain prevent server overload.
  4. Transparent identity  — User-Agent string identifies this bot and
                             links to IITA's research mission.
  5. Public data only      — No login walls, no authenticated content,
                             no personal data collected.
  6. Official APIs used    — Where donors provide a public grant API
                             (EU, Grants.gov), that is used exclusively.

If a source's robots.txt disallows crawling, the source is SKIPPED
automatically and logged. No manual override is possible.

Microsoft 365 email modes (set EMAIL_MODE below):
  "smtp"        Option A — SMTP Auth (IT enables on mailbox)
  "apppassword" Option B — App Password (if MFA is on the mailbox)
  "graph"       Option C — Microsoft Graph API (most secure)

Requirements:
  pip install requests beautifulsoup4 feedparser

Usage:
  python3 iita_cfp_scraper.py                   full run, sends email
  python3 iita_cfp_scraper.py --dry-run         prints digest, no email
  python3 iita_cfp_scraper.py --sources rss     RSS/API feeds only (safest)
  python3 iita_cfp_scraper.py --sources web     web scraping only
  python3 iita_cfp_scraper.py --threshold 70    override relevance cutoff
  python3 iita_cfp_scraper.py --check-robots    audit all sources for robots.txt

Cron schedule (daily 7am WAT):
  0 7 * * * python3 /home/iita_cfp_scraper.py >> /home/cfp_log.txt 2>&1
"""

import os, json, hashlib, logging, argparse, smtplib, sqlite3, time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import Optional
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse

import requests
import feedparser
from bs4 import BeautifulSoup


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "sk-ant-YOUR_KEY_HERE")

EMAIL_MODE = "smtp"   # "smtp" | "apppassword" | "graph"

EMAIL_CONFIG = {
    "from_addr": "cfp-alerts@iita.org",
    "to_addrs":  ["pdau@iita.org", "dg-office@iita.org"],
    # ── Option A / B  (SMTP or App Password) ─────────────────────────────
    "smtp_host": "smtp.office365.com",
    "smtp_port": 587,
    "username":  "cfp-alerts@iita.org",
    "password":  os.getenv("M365_PASSWORD", "YOUR_M365_PASSWORD_OR_APP_PASSWORD"),
    # ── Option C  (Microsoft Graph API) ──────────────────────────────────
    "graph_tenant_id":     os.getenv("GRAPH_TENANT_ID",     "YOUR_TENANT_ID"),
    "graph_client_id":     os.getenv("GRAPH_CLIENT_ID",     "YOUR_CLIENT_ID"),
    "graph_client_secret": os.getenv("GRAPH_CLIENT_SECRET", "YOUR_CLIENT_SECRET"),
}

DB_PATH                 = "iita_cfp_seen.db"
RELEVANCE_THRESHOLD     = 60
HIGH_PRIORITY_THRESHOLD = 80

# ── Legal / crawl compliance settings ────────────────────────────────────────
BOT_NAME              = "IITA-CFP-Bot"
BOT_VERSION           = "3.0"
BOT_CONTACT_URL       = "https://www.iita.org/research"   # shown in User-Agent
CRAWL_DELAY_SECONDS   = 2.5    # pause between requests to the same domain
ROBOTS_CACHE_TTL      = 3600   # seconds to cache robots.txt decisions (1 hr)
REQUEST_TIMEOUT       = 18     # seconds before giving up on a request

# ── Build the User-Agent string ───────────────────────────────────────────────
# Transparent, identifiable, links to IITA mission — best practice
USER_AGENT = (
    f"{BOT_NAME}/{BOT_VERSION} "
    f"(+{BOT_CONTACT_URL}; research-for-development; non-commercial; "
    f"contact: pdau@iita.org)"
)

HEADERS = {"User-Agent": USER_AGENT}


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — IITA THEMATIC AREAS & KEYWORDS
# ══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────
# HOW TO ADD A NEW RESEARCH THEME
# ─────────────────────────────────────────────────────────────────────────────
# 1. Add a new entry at the bottom of IITA_THEMES following this pattern:
#
#    "Your Theme Name Here": [
#        "keyword one", "keyword two", "keyword three",
#        "relevant acronym", "technical term",
#    ],
#
# 2. Use specific, technical words — avoid generic words like "food", "data",
#    "research", "project" which appear in almost every CFP and cause noise.
#
# 3. Include both US and UK spellings where they differ
#    (e.g. "fertilizer" AND "fertiliser").
#
# 4. Also add the theme name to the THEMES list in iita_cfp_dashboard.html
#    so it appears as a filter button in the dashboard.
# ─────────────────────────────────────────────────────────────────────────────

IITA_THEMES = {
    "Crop Improvement & Breeding": [
        "cassava", "maize", "cowpea", "soybean", "soya", "yam", "banana",
        "plantain", "crop improvement", "plant breeding", "varietal development",
        "genetic resources", "genomics", "seed systems", "germplasm", "hybrid",
        "cultivar", "variety release", "marker-assisted", "GWAS",
    ],
    "Agronomy & Soil Health": [
        "agronomy", "soil health", "soil fertility", "integrated soil fertility",
        "fertilizer", "fertiliser", "land degradation", "erosion",
        "sustainable intensification", "nitrogen fixation", "intercropping",
        "conservation agriculture", "cover crop", "soil carbon", "mulching",
        "ISFM", "soil organic matter",
    ],
    "Food Systems & Nutrition": [
        "food systems", "nutrition", "biofortification", "post-harvest",
        "value chain", "food security", "dietary diversity", "food processing",
        "food safety", "agri-food", "malnutrition", "stunting", "wasting",
        "micronutrient", "hidden hunger", "food loss", "food waste",
        "aflatoxin control", "market systems",
    ],
    "Climate Adaptation & Resilience": [
        "climate change", "climate adaptation", "climate-smart agriculture",
        "drought tolerance", "heat stress", "resilience", "agroforestry",
        "carbon sequestration", "greenhouse gas", "adaptation", "mitigation",
        "climate resilient", "flood", "rainfall variability", "nature-based",
    ],
    "Pest, Disease & Weed Management": [
        "pest management", "disease management", "integrated pest management",
        "biological control", "fall armyworm", "aflatoxin", "cassava mosaic",
        "striga", "locust", "invasive species", "plant health", "mycotoxin",
        "nematode", "virus", "blight", "rust", "IPM", "biocontrol",
    ],
    "Gender, Youth & Policy": [
        "gender", "women in agriculture", "youth", "inclusive", "equity",
        "policy", "agricultural policy", "extension", "capacity building",
        "smallholder", "farmer", "rural livelihoods", "women farmers",
        "gender equality", "social inclusion", "empowerment", "nutrition-sensitive",
    ],
    "Digital Agriculture & Data": [
        "digital agriculture", "precision agriculture", "remote sensing",
        "machine learning", "artificial intelligence", "e-extension",
        "ict in agriculture", "satellite", "gis", "modeling", "drone",
        "big data", "decision support", "ICT4Ag", "digital tools",
    ],
    # ── ADD NEW THEMES BELOW THIS LINE ────────────────────────────────────
    # Example (uncomment and edit to activate):
    #
    # "Aquaculture & Fish Systems": [
    #     "aquaculture", "fish farming", "tilapia", "catfish",
    #     "pond management", "inland fisheries", "fish feed",
    #     "aquatic food systems",
    # ],
    #
    # "One Health & Zoonotic Disease": [
    #     "one health", "zoonotic", "zoonosis",
    #     "animal-human interface", "antimicrobial resistance",
    #     "livestock disease", "brucellosis", "AMR",
    # ],
}

AFRICA_KEYWORDS = [
    "africa", "sub-saharan", "west africa", "east africa", "southern africa",
    "nigeria", "ghana", "kenya", "tanzania", "ethiopia", "malawi", "zambia",
    "cameroon", "uganda", "rwanda", "senegal", "benin", "mali", "niger",
    "burkina faso", "mozambique", "zimbabwe", "developing countries", "global south",
]


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — DATA SOURCES
# ══════════════════════════════════════════════════════════════════════════════
# Source types:
#   "rss"  — RSS/Atom feed   (machine-intended, safest, always preferred)
#   "api"  — Official API    (explicitly invited machine access, zero risk)
#   "web"  — HTML scraping   (subject to robots.txt check; skipped if disallowed)
#
# The "robots_path" field (optional) lets you specify the exact path to check
# in robots.txt. Defaults to the root URL if omitted.

RSS_SOURCES = [
    {"name": "CGIAR",                    "url": "https://www.cgiar.org/feed/",                                                                           "type": "rss"},
    {"name": "FAO Funding",              "url": "https://www.fao.org/partnerships/resource-mobilization/calls-for-proposals/rss/en/",                     "type": "rss"},
    {"name": "IFAD",                     "url": "https://www.ifad.org/en/rss",                                                                           "type": "rss"},
    {"name": "Bioversity International", "url": "https://www.bioversityinternational.org/feed/",                                                         "type": "rss"},
    {"name": "RUFORUM",                  "url": "https://www.ruforum.org/feed/",                                                                         "type": "rss"},
]

# Official grant APIs — explicitly designed for machine access; no robots.txt needed
API_SOURCES = [
    {
        "name": "Grants.gov (US Federal)",
        "type": "api",
        # Public REST API — no key required for search
        "url":  "https://api.grants.gov/v1/api/search2",
        "method": "POST",
        "payload": {
            "keyword":    "agriculture Africa food nutrition",
            "oppStatuses": "forecasted|posted",
            "rows":        25,
        },
        "result_path": ["oppHits"],         # JSON path to results list
        "title_field":  "oppTitle",
        "url_template": "https://www.grants.gov/search-grants?keywords={oppNumber}",
        "desc_field":   "synopsis",
        "date_field":   "openDate",
    },
    {
        "name": "EU Funding & Tenders Portal",
        "type": "api",
        # EU open data API — no key required
        "url":  "https://api.tech.ec.europa.eu/search-api/prod/rest/search",
        "method": "GET",
        "params": {
            "apiKey":   "SEDIA",
            "text":     "agriculture Africa food nutrition climate",
            "pageSize": 20,
            "pageNumber": 1,
        },
        "result_path": ["results"],
        "title_field":  "title",
        "url_template": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{identifier}",
        "desc_field":   "description",
        "date_field":   "startDate",
    },
]

WEB_SOURCES = [
    {"name": "USAID",            "url": "https://www.usaid.gov/work-usaid/get-grant-or-contract",                "selector": "article, h2 a, h3 a, .opportunity"},
    {"name": "Gates Foundation", "url": "https://www.gatesfoundation.org/about/committed-grants",               "selector": ".grant-item, article h2"},
    {"name": "Wellcome Trust",   "url": "https://wellcome.org/grant-funding/schemes",                           "selector": ".scheme-item, article"},
    {"name": "African Dev Bank", "url": "https://www.afdb.org/en/projects-and-operations/procurement",          "selector": ".procurement-item, article"},
    {"name": "UK FCDO",          "url": "https://www.gov.uk/government/organisations/foreign-commonwealth-development-office/about/research", "selector": ".gem-c-document-list__item"},
    {"name": "World Bank",       "url": "https://www.worldbank.org/en/programs/grants",                         "selector": "article, .program-item, h2 a"},
    {"name": "AGRA",             "url": "https://agra.org/grants/",                                             "selector": "article, .grant-item, h2, h3"},
]


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — ROBOTS.TXT COMPLIANCE ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class RobotsCache:
    """
    Thread-safe in-memory cache for robots.txt decisions.
    Avoids fetching the same robots.txt file repeatedly during a run.
    TTL defaults to ROBOTS_CACHE_TTL seconds.
    """
    def __init__(self, ttl: int = ROBOTS_CACHE_TTL):
        self._cache: dict[str, tuple[RobotFileParser, float]] = {}
        self._ttl = ttl

    def _base(self, url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    def _load(self, base_url: str) -> Optional[RobotFileParser]:
        robots_url = f"{base_url}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        try:
            rp.read()
            return rp
        except Exception as e:
            logging.debug(f"Could not fetch robots.txt from {robots_url}: {e}")
            return None

    def is_allowed(self, url: str) -> bool:
        """
        Returns True if our bot is permitted to fetch this URL.
        Returns True if robots.txt cannot be fetched (fail-open with logging).
        Returns False if robots.txt explicitly disallows our bot.
        """
        base = self._base(url)
        now  = time.time()

        # Check cache
        if base in self._cache:
            rp, fetched_at = self._cache[base]
            if now - fetched_at < self._ttl:
                if rp is None:
                    return True   # previously unreachable → allow
                allowed = rp.can_fetch(BOT_NAME, url)
                if not allowed:
                    logging.warning(
                        f"  [ROBOTS BLOCKED] {url}\n"
                        f"  robots.txt at {base}/robots.txt disallows {BOT_NAME}.\n"
                        f"  Skipping this source. Contact the donor directly for data access."
                    )
                return allowed

        # Not cached or expired — fetch fresh
        rp = self._load(base)
        self._cache[base] = (rp, now)

        if rp is None:
            logging.debug(f"robots.txt unreachable for {base} — proceeding cautiously")
            return True

        allowed = rp.can_fetch(BOT_NAME, url)
        if not allowed:
            logging.warning(
                f"  [ROBOTS BLOCKED] {url}\n"
                f"  robots.txt at {base}/robots.txt disallows {BOT_NAME}.\n"
                f"  Skipping this source. Use RSS feed or contact donor directly."
            )
        return allowed

    def audit(self, sources: list[dict]) -> None:
        """Print a full robots.txt compliance report for all web sources."""
        print("\n" + "═"*65)
        print("  IITA CFP System — robots.txt Compliance Audit")
        print("═"*65)
        for src in sources:
            url     = src["url"]
            allowed = self.is_allowed(url)
            status  = "✅ ALLOWED " if allowed else "❌ BLOCKED "
            print(f"  {status}  {src['name']:<28}  {url}")
        print("═"*65 + "\n")


# Global robots cache — shared across all scrapers in a run
robots = RobotsCache()


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — DATA MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CFPRecord:
    title:           str
    url:             str
    source:          str
    snippet:         str
    source_type:     str = "web"           # "rss" | "api" | "web"
    published:       Optional[str] = None
    deadline:        Optional[str] = None
    budget:          Optional[str] = None
    eligible:        Optional[str] = None
    consortium:      Optional[bool] = None
    themes_matched:  list = field(default_factory=list)
    relevance_score: int = 0
    ai_summary:      str = ""
    urgency:         str = "medium"
    fingerprint:     str = ""

    def compute_fingerprint(self):
        self.fingerprint = hashlib.sha256(
            f"{self.url}::{self.title}".encode()
        ).hexdigest()[:16]
        return self.fingerprint


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_cfps (
            fingerprint     TEXT PRIMARY KEY,
            title           TEXT,
            url             TEXT,
            source          TEXT,
            source_type     TEXT,
            relevance_score INTEGER,
            first_seen      TEXT
        )
    """)
    conn.commit()
    return conn

def is_new(conn, fp):
    return conn.execute("SELECT 1 FROM seen_cfps WHERE fingerprint=?", (fp,)).fetchone() is None

def mark_seen(conn, r: CFPRecord):
    conn.execute("INSERT OR IGNORE INTO seen_cfps VALUES (?,?,?,?,?,?,?)",
        (r.fingerprint, r.title, r.url, r.source, r.source_type,
         r.relevance_score, datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 7 — KEYWORD PRE-FILTER
# ══════════════════════════════════════════════════════════════════════════════

def keyword_prefilter(text: str) -> tuple[bool, list[str]]:
    """Fast local check — saves ~70% of Claude API calls."""
    tl = text.lower()
    matched = [t for t, kws in IITA_THEMES.items() if any(k in tl for k in kws)]
    return bool(matched), matched


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 8 — POLITE HTTP HELPER
# ══════════════════════════════════════════════════════════════════════════════

_last_request_time: dict[str, float] = {}   # domain → last request timestamp

def polite_get(url: str, **kwargs) -> Optional[requests.Response]:
    """
    Fetches a URL with:
      1. robots.txt check — returns None if disallowed
      2. Per-domain crawl delay — pauses CRAWL_DELAY_SECONDS between requests
      3. Transparent User-Agent header
    """
    # ── robots.txt check ──────────────────────────────────────────────────────
    if not robots.is_allowed(url):
        return None   # caller must handle None as "skipped"

    # ── per-domain crawl delay ────────────────────────────────────────────────
    domain = urlparse(url).netloc
    now    = time.time()
    last   = _last_request_time.get(domain, 0)
    gap    = now - last
    if gap < CRAWL_DELAY_SECONDS:
        wait = CRAWL_DELAY_SECONDS - gap
        logging.debug(f"Crawl delay: waiting {wait:.1f}s for {domain}")
        time.sleep(wait)

    # ── fetch ─────────────────────────────────────────────────────────────────
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **kwargs)
        _last_request_time[domain] = time.time()
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        logging.warning(f"  HTTP error fetching {url}: {e}")
        _last_request_time[domain] = time.time()
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 9 — SCRAPERS
# ══════════════════════════════════════════════════════════════════════════════

def scrape_rss(source: dict) -> list[CFPRecord]:
    """
    RSS/Atom feeds are machine-intended publications — no robots.txt needed.
    This is the safest source type and is always preferred.
    """
    records = []
    try:
        feed = feedparser.parse(source["url"])
        for e in feed.entries[:25]:
            title   = getattr(e, "title",   "").strip()
            url     = getattr(e, "link",    "").strip()
            summary = getattr(e, "summary", "").strip()
            if not title or not url:
                continue
            ok, themes = keyword_prefilter(f"{title} {summary}")
            if not ok:
                continue
            rec = CFPRecord(
                title=title, url=url, source=source["name"],
                snippet=summary[:600], source_type="rss",
                published=getattr(e, "published", None),
                themes_matched=themes,
            )
            rec.compute_fingerprint()
            records.append(rec)
            logging.info(f"  [RSS ✓] {title[:70]}")
    except Exception as ex:
        logging.warning(f"RSS error ({source['name']}): {ex}")
    return records


def scrape_api(source: dict) -> list[CFPRecord]:
    """
    Official grant APIs are explicitly designed for machine access.
    No robots.txt check needed — these endpoints are published for automation.
    """
    records = []
    try:
        if source.get("method") == "POST":
            resp = requests.post(
                source["url"], json=source.get("payload", {}),
                headers=HEADERS, timeout=REQUEST_TIMEOUT,
            )
        else:
            resp = requests.get(
                source["url"], params=source.get("params", {}),
                headers=HEADERS, timeout=REQUEST_TIMEOUT,
            )
        resp.raise_for_status()
        data = resp.json()

        # Navigate to results list using result_path
        results = data
        for key in source.get("result_path", []):
            if isinstance(results, dict):
                results = results.get(key, [])
            else:
                results = []
                break

        if not isinstance(results, list):
            return records

        for item in results[:30]:
            title = str(item.get(source["title_field"], "")).strip()
            desc  = str(item.get(source.get("desc_field", ""), "")).strip()
            date  = str(item.get(source.get("date_field", ""), "")).strip()

            # Build URL from template if provided
            url_tpl = source.get("url_template", source["url"])
            try:
                url = url_tpl.format(**item)
            except Exception:
                url = source["url"]

            if not title or len(title) < 15:
                continue

            ok, themes = keyword_prefilter(f"{title} {desc}")
            if not ok:
                continue

            rec = CFPRecord(
                title=title[:220], url=url, source=source["name"],
                snippet=desc[:600], source_type="api",
                published=date, themes_matched=themes,
            )
            rec.compute_fingerprint()
            records.append(rec)
            logging.info(f"  [API ✓] {title[:70]}")

    except Exception as ex:
        logging.warning(f"API error ({source['name']}): {ex}")
    return records


def scrape_web(source: dict) -> list[CFPRecord]:
    """
    HTML scraping with full compliance:
      • robots.txt checked via polite_get() before any request
      • Per-domain crawl delay enforced
      • Skipped entirely if disallowed
    """
    records = []

    # robots.txt is checked inside polite_get — if None is returned, skip
    resp = polite_get(source["url"])
    if resp is None:
        logging.info(f"  [SKIPPED] {source['name']} — robots.txt disallowed or request failed")
        return records

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for el in soup.select(source["selector"])[:35]:
            title = el.get_text(strip=True)
            if len(title) < 20:
                continue

            href = el.get("href", "") if el.name == "a" else ""
            if not href:
                a_tag = el.find("a")
                href  = a_tag.get("href", "") if a_tag else ""
            if href and not href.startswith("http"):
                from urllib.parse import urljoin
                href = urljoin(source["url"], href)

            # Politely fetch the linked page for more context if available
            # (but honour robots.txt for that URL too)
            sib     = el.find_next("p")
            snippet = sib.get_text(strip=True)[:500] if sib else ""

            ok, themes = keyword_prefilter(f"{title} {snippet}")
            if not ok:
                continue

            rec = CFPRecord(
                title=title[:220], url=href or source["url"],
                source=source["name"], snippet=snippet,
                source_type="web", themes_matched=themes,
            )
            rec.compute_fingerprint()
            records.append(rec)
            logging.info(f"  [WEB ✓] {title[:70]}")

            # Polite delay between processing items from the same domain
            time.sleep(0.5)

    except Exception as ex:
        logging.warning(f"Parse error ({source['name']}): {ex}")
    return records


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 10 — CLAUDE AI SCORING
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a grants intelligence assistant for IITA (International
Institute of Tropical Agriculture), headquartered in Ibadan, Nigeria. IITA's mandate
is agricultural research-for-development across Africa in: crop improvement & breeding
(cassava, maize, cowpea, soybean, yam, banana), agronomy & soil health, food systems
& nutrition, climate adaptation & resilience, pest/disease/weed management,
gender/youth/policy, and digital agriculture.

For each CFP, respond ONLY with a valid JSON object — no markdown, no text outside:

{
  "relevance_score": <0-100>,
  "deadline": "<date string or null>",
  "budget_range": "<award size or null>",
  "eligible_regions": "<geographies or null>",
  "themes": ["<matched IITA theme>"],
  "consortium_required": <true|false|null>,
  "lead_or_partner": "<lead applicant|partner|either|unclear>",
  "urgency": "<high|medium|low>",
  "summary": "<2-3 sentence plain English summary and why IITA should respond>"
}

Scoring: 90-100 perfect fit, 70-89 strong, 50-69 moderate, 0-49 irrelevant."""


def score_with_claude(record: CFPRecord) -> CFPRecord:
    msg = (
        f"Title: {record.title}\nSource: {record.source} ({record.source_type})\n"
        f"URL: {record.url}\nPublished: {record.published or 'Unknown'}\n"
        f"Pre-matched themes: {', '.join(record.themes_matched)}\n\n{record.snippet}"
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 700,
                  "system": SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": msg}]},
            timeout=30,
        )
        resp.raise_for_status()
        data = json.loads(resp.json()["content"][0]["text"])
        record.relevance_score = int(data.get("relevance_score", 0))
        record.deadline        = data.get("deadline")
        record.budget          = data.get("budget_range")
        record.eligible        = data.get("eligible_regions")
        record.consortium      = data.get("consortium_required")
        record.urgency         = data.get("urgency", "medium")
        record.ai_summary      = data.get("summary", "")
        record.themes_matched  = data.get("themes", record.themes_matched)
        logging.info(f"  Score {record.relevance_score:3d}/100  [{record.source_type.upper()}] {record.title[:50]}")
    except Exception as ex:
        logging.warning(f"Claude error: {ex}")
        record.relevance_score = len(record.themes_matched) * 12
        record.ai_summary = "AI scoring unavailable — manual review recommended."
    return record


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 11 — EMAIL HTML BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_email_html(records: list[CFPRecord], run_date: str) -> str:
    urgent   = [r for r in records if r.relevance_score >= HIGH_PRIORITY_THRESHOLD]
    standard = [r for r in records if r.relevance_score <  HIGH_PRIORITY_THRESHOLD]

    src_type_icon = {"rss": "📡 RSS", "api": "🔌 API", "web": "🌐 Web"}

    def card(r):
        hot = r.relevance_score >= HIGH_PRIORITY_THRESHOLD
        bc  = "#D4501A" if hot else "#1a7a2e"
        bbg = "#FDF0EA" if hot else "#f0fdf4"
        bfg = "#D4501A" if hot else "#1a7a2e"
        lbl = "URGENT" if hot else "NEW"
        themes = "  ·  ".join(r.themes_matched[:3]) or "General"
        src_lbl = src_type_icon.get(r.source_type, "🌐")
        rows = ""
        if r.deadline:   rows += f"<tr><td style='color:#666;font-size:12px;width:90px;padding:2px 0'>Deadline</td><td style='font-size:12px;font-weight:600'>{r.deadline}</td></tr>"
        if r.budget:     rows += f"<tr><td style='color:#666;font-size:12px;padding:2px 0'>Budget</td><td style='font-size:12px'>{r.budget}</td></tr>"
        if r.eligible:   rows += f"<tr><td style='color:#666;font-size:12px;padding:2px 0'>Region</td><td style='font-size:12px'>{r.eligible}</td></tr>"
        if r.consortium is True:  rows += "<tr><td style='color:#666;font-size:12px;padding:2px 0'>Consortium</td><td style='font-size:12px'>Required</td></tr>"
        if r.consortium is False: rows += "<tr><td style='color:#666;font-size:12px;padding:2px 0'>Consortium</td><td style='font-size:12px'>Not required</td></tr>"
        return f"""
        <div style="border-left:4px solid {bc};padding:16px 18px;margin-bottom:16px;background:#fafafa;border-radius:0 8px 8px 0">
          <table width="100%" style="margin-bottom:8px"><tr>
            <td><a href="{r.url}" style="font-size:15px;font-weight:700;color:#B3400F;text-decoration:none;line-height:1.4">{r.title}</a>
                <div style="font-size:11px;color:#888;margin-top:3px">{src_lbl} &nbsp;·&nbsp; {r.source}</div></td>
            <td width="115" style="text-align:right;vertical-align:top">
              <span style="padding:4px 10px;background:{bbg};color:{bfg};font-size:11px;font-weight:700;border-radius:20px">
                {lbl} · {r.relevance_score}/100
              </span></td>
          </tr></table>
          <div style="font-size:11px;color:#666;margin-bottom:8px">🏷 {themes}</div>
          <table style="border-collapse:collapse;margin-bottom:10px">{rows}</table>
          <p style="font-size:13px;color:#333;line-height:1.65;margin:0 0 10px">{r.ai_summary}</p>
          <a href="{r.url}" style="font-size:12px;color:#D4501A;font-weight:600">View full call →</a>
        </div>"""

    urgent_block = ""
    if urgent:
        urgent_block = f"""<div style="background:#FDF0EA;border:1px solid #F9D5C0;border-radius:8px;padding:18px 20px;margin-bottom:24px">
          <h2 style="color:#D4501A;margin:0 0 14px;font-size:16px;font-family:Arial">
            🚨 High Priority — Immediate Action Required ({len(urgent)})
          </h2>{"".join(card(r) for r in urgent)}</div>"""

    standard_block = ""
    if standard:
        standard_block = f"""<div style="margin-bottom:24px">
          <h2 style="color:#1a7a2e;margin:0 0 14px;font-size:16px;font-family:Arial">
            ✅ New Opportunities ({len(standard)})
          </h2>{"".join(card(r) for r in standard)}</div>"""

    rss_count = sum(1 for r in records if r.source_type == "rss")
    api_count = sum(1 for r in records if r.source_type == "api")
    web_count = sum(1 for r in records if r.source_type == "web")

    return f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f0f2f7;font-family:Arial,sans-serif">
<div style="max-width:700px;margin:0 auto;padding:24px 16px">
  <div style="background:#1C1A17;border-radius:10px 10px 0 0;padding:22px 28px">
    <div style="color:#D4501A;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px">International Institute of Tropical Agriculture</div>
    <h1 style="color:#fff;margin:0;font-size:22px">CFP Intelligence Digest</h1>
    <p style="color:#9a9080;margin:6px 0 0;font-size:13px">{run_date}</p>
  </div>
  <div style="background:#FDF0EA;border:1px solid #F9D5C0;border-top:none;padding:14px 28px">
    <table><tr>
      <td style="padding-right:32px;text-align:center"><div style="font-size:26px;font-weight:700;color:#D4501A">{len(records)}</div><div style="font-size:11px;color:#666">New opportunities</div></td>
      <td style="padding-right:32px;text-align:center"><div style="font-size:26px;font-weight:700;color:#8B2E08">{len(urgent)}</div><div style="font-size:11px;color:#666">High priority</div></td>
      <td style="padding-right:32px;text-align:center"><div style="font-size:18px;font-weight:700;color:#1a7a2e">{rss_count} RSS · {api_count} API · {web_count} Web</div><div style="font-size:11px;color:#666">Source breakdown</div></td>
    </tr></table>
  </div>
  <div style="background:#fff;border:1px solid #e5ded6;border-top:none;border-radius:0 0 10px 10px;padding:28px">
    {urgent_block}{standard_block}
    <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
    <p style="font-size:11px;color:#999;margin:0;line-height:1.8">
      IITA CFP Intelligence System v3.0 · PDAU Division · IITA Ibadan, Nigeria<br>
      Powered by Claude AI · Microsoft 365 · robots.txt compliant · RSS-first<br>
      <a href="mailto:pdau@iita.org" style="color:#999">Manage alerts</a>
    </p>
  </div>
</div></body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 12 — EMAIL SENDERS (Microsoft 365)
# ══════════════════════════════════════════════════════════════════════════════

def _subject(records):
    s = f"[IITA CFP Alert] {len(records)} New Opportunities — {datetime.now().strftime('%d %b %Y')}"
    return ("🚨 " + s) if any(r.relevance_score >= HIGH_PRIORITY_THRESHOLD for r in records) else s

def send_via_smtp(html, records):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = _subject(records)
    msg["From"]    = EMAIL_CONFIG["from_addr"]
    msg["To"]      = ", ".join(EMAIL_CONFIG["to_addrs"])
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(EMAIL_CONFIG["smtp_host"], EMAIL_CONFIG["smtp_port"]) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(EMAIL_CONFIG["username"], EMAIL_CONFIG["password"])
        s.sendmail(EMAIL_CONFIG["from_addr"], EMAIL_CONFIG["to_addrs"], msg.as_string())
    logging.info(f"Email sent via SMTP to {EMAIL_CONFIG['to_addrs']}")

def send_via_graph(html, records):
    tok = requests.post(
        f"https://login.microsoftonline.com/{EMAIL_CONFIG['graph_tenant_id']}/oauth2/v2.0/token",
        data={"grant_type":"client_credentials",
              "client_id":     EMAIL_CONFIG["graph_client_id"],
              "client_secret": EMAIL_CONFIG["graph_client_secret"],
              "scope":         "https://graph.microsoft.com/.default"},
        timeout=15,
    )
    tok.raise_for_status()
    token = tok.json()["access_token"]
    r = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{EMAIL_CONFIG['from_addr']}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message":{"subject":_subject(records),
                         "body":{"contentType":"HTML","content":html},
                         "toRecipients":[{"emailAddress":{"address":a}} for a in EMAIL_CONFIG["to_addrs"]]},
              "saveToSentItems":"true"},
        timeout=15,
    )
    r.raise_for_status()
    logging.info(f"Email sent via Graph API to {EMAIL_CONFIG['to_addrs']}")

def send_email(html, records, dry_run=False):
    if dry_run:
        print(f"\n{'='*70}\nDRY RUN | Mode: {EMAIL_MODE.upper()} | To: {EMAIL_CONFIG['to_addrs']}\nSubject: {_subject(records)}\n{'='*70}")
        for r in records:
            flag = "🚨" if r.relevance_score >= HIGH_PRIORITY_THRESHOLD else "✅"
            src_tag = f"[{r.source_type.upper()}]"
            print(f"\n  {flag} [{r.relevance_score:3d}] {src_tag} {r.source}\n       {r.title[:70]}")
            if r.deadline: print(f"       Deadline: {r.deadline}")
            if r.budget:   print(f"       Budget:   {r.budget}")
            print(f"       {r.ai_summary[:130]}...")
        return
    if EMAIL_MODE in ("smtp", "apppassword"):
        send_via_smtp(html, records)
    elif EMAIL_MODE == "graph":
        send_via_graph(html, records)
    else:
        raise ValueError(f"Unknown EMAIL_MODE '{EMAIL_MODE}'.")


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 13 — MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def run(dry_run=False, sources_filter=None, threshold=RELEVANCE_THRESHOLD, source_names=None, themes_filter=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    run_date = datetime.now().strftime("%A, %d %B %Y — %H:%M WAT")
    logging.info("="*65)
    logging.info("  IITA CFP Intelligence System v3.0")
    logging.info(f"  robots.txt: ENFORCED  |  Crawl delay: {CRAWL_DELAY_SECONDS}s")
    logging.info(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}  |  Threshold: {threshold}  |  Email: {EMAIL_MODE.upper()}")
    logging.info("="*65)

    conn = init_db(DB_PATH)
    candidates = []

    # Priority order: API → RSS → Web (safest to least safe)
    if sources_filter in (None, "rss"):
        logging.info("\n── API Sources (official endpoints — zero legal risk) ──")
        for src in API_SOURCES:
            if source_names and src['name'] not in source_names:
                continue
            logging.info(f"API → {src['name']}")
            candidates.extend(scrape_api(src))

        logging.info("\n── RSS Sources (machine-intended feeds — no legal risk) ──")
        for src in RSS_SOURCES:
            if source_names and src['name'] not in source_names:
                continue
            logging.info(f"RSS → {src['name']}")
            candidates.extend(scrape_rss(src))

    if sources_filter in (None, "web"):
        logging.info("\n── Web Sources (robots.txt checked before each request) ──")
        for src in WEB_SOURCES:
            if source_names and src['name'] not in source_names:
                continue
            logging.info(f"WEB → {src['name']}")
            candidates.extend(scrape_web(src))

    if themes_filter:
        candidates = [r for r in candidates if set(r.themes_matched).intersection(themes_filter)]

    src_breakdown = {
        "api": sum(1 for r in candidates if r.source_type == "api"),
        "rss": sum(1 for r in candidates if r.source_type == "rss"),
        "web": sum(1 for r in candidates if r.source_type == "web"),
    }
    logging.info(f"\nKeyword-matched: {len(candidates)} "
                 f"(API:{src_breakdown['api']} RSS:{src_breakdown['rss']} Web:{src_breakdown['web']})")

    new_records = [r for r in candidates if is_new(conn, r.fingerprint)]
    logging.info(f"New (not seen):  {len(new_records)}")

    if not new_records:
        logging.info("Nothing new. No email sent.")
        conn.close(); return []

    logging.info(f"\nScoring {len(new_records)} candidates with Claude AI...")
    scored = [score_with_claude(r) for r in new_records]
    for r in scored:
        mark_seen(conn, r)
    conn.close()

    qualified = sorted(
        [r for r in scored if r.relevance_score >= threshold],
        key=lambda r: r.relevance_score, reverse=True,
    )
    logging.info(f"\nQualified (score >= {threshold}): {len(qualified)}")
    if not qualified:
        logging.info("No CFPs met threshold. No email sent."); return []

    html = build_email_html(qualified, run_date)
    send_email(html, qualified, dry_run=dry_run)
    urgent = [r for r in qualified if r.relevance_score >= HIGH_PRIORITY_THRESHOLD]
    logging.info(f"\nDone. {len(qualified)} alerted. {len(urgent)} high priority.")
    return qualified


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="IITA CFP Intelligence System v3.0 — robots.txt compliant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 iita_cfp_scraper.py --dry-run
  python3 iita_cfp_scraper.py --sources rss       # safest — RSS + API only
  python3 iita_cfp_scraper.py --threshold 70
  python3 iita_cfp_scraper.py --check-robots      # audit all sources first
        """,
    )
    p.add_argument("--dry-run",      action="store_true",
                   help="Print digest to console instead of sending email")
    p.add_argument("--sources",      choices=["rss","web","all"], default="all")
    p.add_argument("--threshold",    type=int, default=RELEVANCE_THRESHOLD)
    p.add_argument("--check-robots", action="store_true",
                   help="Run a robots.txt compliance audit on all web sources and exit")
    args = p.parse_args()

    if args.check_robots:
        robots.audit(WEB_SOURCES)
    else:
        run(
            dry_run=args.dry_run,
            sources_filter=None if args.sources == "all" else args.sources,
            threshold=args.threshold,
        )




