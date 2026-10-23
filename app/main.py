from __future__ import annotations
import hmac, json, os, re, sqlite3, sys, threading, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from urllib.parse import urlparse
from dotenv import load_dotenv
from fastapi import Body
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
ADMIN_TOKEN = os.getenv('IITA_CFP_ADMIN_TOKEN', '').strip()
admin_bearer = HTTPBearer(auto_error=False)
DATA_DIR = Path(os.getenv('IITA_CFP_DATA_DIR', str(ROOT)))
if not DATA_DIR.is_absolute():
    DATA_DIR = ROOT / DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_file(env_name: str, default_name: str) -> Path:
    configured = os.getenv(env_name, '').strip()
    path = Path(configured) if configured else DATA_DIR / default_name
    if not path.is_absolute():
        path = DATA_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


DB_PATH = data_file('IITA_CFP_DATABASE', 'iita_cfp_app.db')
STATUSES = ('new', 'reviewing', 'submitted', 'won')
scan_state = {'id': None, 'status': 'idle', 'message': 'No scan has run yet', 'started_at': None, 'finished_at': None, 'new_count': 0}
scan_lock = threading.Lock()
schedule_stop = threading.Event()
schedule_thread = None
WAT = timezone(timedelta(hours=1), 'WAT')
DEFAULT_SCHEDULE = {'enabled': True, 'time': '07:00', 'send_email': False}

def connect():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS opportunities (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',
        published TEXT, deadline TEXT, budget TEXT, consortium INTEGER, eligible_regions TEXT,
        themes TEXT NOT NULL DEFAULT '[]', score INTEGER NOT NULL DEFAULT 0, urgency TEXT NOT NULL DEFAULT 'medium',
        summary TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new', source_type TEXT NOT NULL DEFAULT 'web',
        scan_date TEXT NOT NULL, created_at TEXT NOT NULL)''')

def get_schedule_config():
    config = read_runtime_config()
    return {**DEFAULT_SCHEDULE, **config.get('schedule', {})}


def scheduled_scan_loop():
    while not schedule_stop.wait(20):
        try:
            schedule = get_schedule_config()
            if not schedule.get('enabled', True):
                continue
            hour, minute = (int(part) for part in schedule.get('time', '07:00').split(':', 1))
            now = datetime.now(WAT)
            if (now.hour, now.minute) < (hour, minute):
                continue
            today = now.date().isoformat()
            if schedule.get('last_run_date') == today:
                continue
            scraper = load_scraper()
            started = launch_scan(None, scraper.RELEVANCE_THRESHOLD, [], [], bool(schedule.get('send_email')), scheduled=True)
            if started:
                config = read_runtime_config()
                config.setdefault('schedule', {}).update({'last_run_date': today})
                write_runtime_config(config)
        except Exception:
            # Keep the scheduler alive; the next poll retries if no scan started.
            continue


@asynccontextmanager
async def lifespan(_):
    global schedule_thread
    init_db()
    schedule_stop.clear()
    schedule_thread = threading.Thread(target=scheduled_scan_loop, name='iita-cfp-daily-scheduler', daemon=True)
    schedule_thread.start()
    yield
    schedule_stop.set()

app = FastAPI(title='IITA CFP Intelligence API', version='1.0.0', lifespan=lifespan)
origins = [o.strip() for o in os.getenv('IITA_CFP_ALLOWED_ORIGINS', 'http://localhost:3000').split(',') if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=['*'], allow_headers=['*'])

class OpportunityCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    source: str = Field(min_length=1, max_length=200)
    url: str = ''
    published: str | None = None
    deadline: str | None = None
    budget: str | None = None
    consortium: bool | None = None
    eligible_regions: str | None = None
    themes: list[str] = Field(default_factory=list)
    score: int = Field(default=0, ge=0, le=100)
    urgency: Literal['high', 'medium', 'low'] = 'medium'
    summary: str = ''
    status: Literal['new', 'reviewing', 'submitted', 'won'] = 'new'
    source_type: Literal['rss', 'api', 'web'] = 'web'
class OpportunityUpdate(BaseModel):
    status: Literal['new', 'reviewing', 'submitted', 'won']

def serialize(row):
    item = dict(row)
    item['themes'] = json.loads(item['themes'])
    item['consortium'] = None if item['consortium'] is None else bool(item['consortium'])
    return item

SCRAPER_CONFIG_PATH = data_file('IITA_CFP_SCRAPER_CONFIG', 'scraper_config.json')


def read_runtime_config():
    if not SCRAPER_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(SCRAPER_CONFIG_PATH.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f'Could not read scraper configuration: {exc}')


def write_runtime_config(config):
    SCRAPER_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding='utf-8')


def load_scraper():
    sys.path.insert(0, str(ROOT))
    import iita_cfp_scraper as scraper
    scraper.DB_PATH = str(data_file('IITA_CFP_SEEN_DATABASE', 'iita_cfp_seen.db'))
    config = read_runtime_config()
    settings = config.get('settings', {})
    setting_map = {'RELEVANCE_THRESHOLD': 'relevance_threshold', 'HIGH_PRIORITY_THRESHOLD': 'high_priority_threshold', 'CRAWL_DELAY_SECONDS': 'crawl_delay_seconds', 'ROBOTS_CACHE_TTL': 'robots_cache_ttl_seconds', 'REQUEST_TIMEOUT': 'request_timeout_seconds', 'EMAIL_MODE': 'email_mode'}
    for attribute, config_key in setting_map.items():
        if config_key in settings:
            setattr(scraper, attribute, settings[config_key])
    sources = config.get('sources', {})
    if 'api' in sources: scraper.API_SOURCES = sources['api']
    if 'rss' in sources: scraper.RSS_SOURCES = sources['rss']
    if 'web' in sources: scraper.WEB_SOURCES = sources['web']
    if 'themes' in config: scraper.IITA_THEMES = config['themes']
    if 'africa_keywords' in config: scraper.AFRICA_KEYWORDS = config['africa_keywords']
    email = config.get('email', {})
    for key in ('from_addr', 'to_addrs', 'smtp_host', 'smtp_port', 'username'):
        if key in email: scraper.EMAIL_CONFIG[key] = email[key]
    scraper.USER_AGENT = (f"{scraper.BOT_NAME}/{scraper.BOT_VERSION} (+{scraper.BOT_CONTACT_URL}; "
                          "research-for-development; non-commercial; contact: pdau@iita.org)")
    scraper.HEADERS = {'User-Agent': scraper.USER_AGENT}
    scraper.robots = scraper.RobotsCache(ttl=scraper.ROBOTS_CACHE_TTL)
    return scraper

@app.get('/api/sources')
def list_sources():
    scraper = load_scraper()
    sources = []
    for source_type, collection in (('api', scraper.API_SOURCES), ('rss', scraper.RSS_SOURCES), ('web', scraper.WEB_SOURCES)):
        for source in collection:
            sources.append({'name': source['name'], 'type': source_type.upper(), 'url': source['url']})
    return {'sources': sources, 'themes': list(scraper.IITA_THEMES.keys()), 'threshold': scraper.RELEVANCE_THRESHOLD, 'high_priority_threshold': scraper.HIGH_PRIORITY_THRESHOLD}

@app.post('/api/robots/audit')
def audit_robots():
    scraper = load_scraper()
    results = []
    for source in scraper.WEB_SOURCES:
        results.append({'name': source['name'], 'url': source['url'], 'allowed': scraper.robots.is_allowed(source['url'])})
    return {'results': results, 'allowed': sum(item['allowed'] for item in results), 'blocked': sum(not item['allowed'] for item in results)}
@app.get('/api/admin/auth/status')
def admin_auth_status():
    return {'configured': bool(ADMIN_TOKEN)}


def require_admin(credentials: HTTPAuthorizationCredentials | None = Depends(admin_bearer)):
    if not ADMIN_TOKEN:
        raise HTTPException(503, 'Admin access is not configured. Set IITA_CFP_ADMIN_TOKEN in backend/.env and restart the API.')
    if credentials is None or credentials.scheme.lower() != 'bearer' or not hmac.compare_digest(credentials.credentials, ADMIN_TOKEN):
        raise HTTPException(401, 'Valid admin bearer token required', headers={'WWW-Authenticate': 'Bearer'})
    return True

@app.post('/api/admin/auth/verify')
def verify_admin(_admin: bool = Depends(require_admin)):
    return {'authenticated': True}
@app.get('/api/health')
def health():
    return {'status': 'ok', 'service': 'IITA CFP Intelligence API'}

@app.get('/api/opportunities')
def list_opportunities(search: str | None = None, status: str | None = None, source: str | None = None,
                       min_score: int = Query(0, ge=0, le=100), limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    clauses, params = ['score >= ?'], [min_score]
    if search:
        clauses.append('(title LIKE ? OR source LIKE ? OR summary LIKE ?)'); params.extend([f'%{search}%'] * 3)
    if status:
        if status not in STATUSES: raise HTTPException(400, 'Invalid status')
        clauses.append('status = ?'); params.append(status)
    if source: clauses.append('source = ?'); params.append(source)
    where = ' AND '.join(clauses)
    with connect() as db:
        total = db.execute('SELECT COUNT(*) FROM opportunities WHERE ' + where, params).fetchone()[0]
        rows = db.execute('SELECT * FROM opportunities WHERE ' + where + ' ORDER BY score DESC, deadline LIMIT ? OFFSET ?', [*params, limit, offset]).fetchall()
    return {'items': [serialize(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset}

@app.post('/api/opportunities', status_code=201)
def create_opportunity(payload: OpportunityCreate):
    now = datetime.now(timezone.utc).isoformat(); values = payload.model_dump(); item_id = uuid.uuid4().hex[:12]
    with connect() as db:
        db.execute('''INSERT INTO opportunities (id,title,source,url,published,deadline,budget,consortium,eligible_regions,themes,score,urgency,summary,status,source_type,scan_date,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (item_id, values['title'], values['source'], values['url'], values['published'], values['deadline'], values['budget'], None if values['consortium'] is None else int(values['consortium']), values['eligible_regions'], json.dumps(values['themes']), values['score'], values['urgency'], values['summary'], values['status'], values['source_type'], now[:10], now))
        row = db.execute('SELECT * FROM opportunities WHERE id=?', (item_id,)).fetchone()
    return serialize(row)

@app.patch('/api/opportunities/{item_id}')
def update_opportunity(item_id: str, payload: OpportunityUpdate):
    with connect() as db:
        cur = db.execute('UPDATE opportunities SET status=? WHERE id=?', (payload.status, item_id))
        if not cur.rowcount: raise HTTPException(404, 'Opportunity not found')
        row = db.execute('SELECT * FROM opportunities WHERE id=?', (item_id,)).fetchone()
    return serialize(row)

@app.delete('/api/opportunities/{item_id}', status_code=204)
def delete_opportunity(item_id: str):
    with connect() as db:
        cur = db.execute('DELETE FROM opportunities WHERE id=?', (item_id,))
        if not cur.rowcount: raise HTTPException(404, 'Opportunity not found')

@app.get('/api/reports/summary')
def report_summary():
    with connect() as db:
        total = db.execute('SELECT COUNT(*) FROM opportunities').fetchone()[0]
        rows = db.execute('SELECT status, COUNT(*) AS count FROM opportunities GROUP BY status').fetchall()
        average = db.execute('SELECT COALESCE(ROUND(AVG(score)),0) FROM opportunities').fetchone()[0]
        sources = db.execute('SELECT source,COUNT(*) AS count FROM opportunities GROUP BY source ORDER BY count DESC LIMIT 8').fetchall()
    by_status = {s: 0 for s in STATUSES}; by_status.update({r['status']: r['count'] for r in rows})
    return {'total': total, 'average_score': average, 'by_status': by_status, 'top_sources': [dict(r) for r in sources], 'last_scan': scan_state}

def run_scraper(scan_id, sources, threshold, source_names, themes, send_email=False, scheduled=False):
    global scan_state
    try:
        scraper = load_scraper()
        records = scraper.run(dry_run=not send_email, sources_filter=sources, threshold=threshold, source_names=source_names or None, themes_filter=themes or None) or []
        opportunity_ids = []
        for record in records:
            item = create_opportunity(OpportunityCreate(title=record.title, source=record.source, url=record.url, published=record.published, deadline=record.deadline, budget=record.budget, consortium=record.consortium, eligible_regions=record.eligible, themes=record.themes_matched, score=record.relevance_score, urgency=record.urgency, summary=record.ai_summary or record.snippet, source_type=record.source_type))
            opportunity_ids.append(item['id'])
        scan_state = {**scan_state, 'status': 'completed', 'message': f'Scan complete: {len(opportunity_ids)} qualifying opportunities', 'finished_at': datetime.now(timezone.utc).isoformat(), 'new_count': len(opportunity_ids), 'opportunity_ids': opportunity_ids, 'digest_available': bool(opportunity_ids), 'dry_run': not send_email, 'scheduled': scheduled}
    except Exception as exc:
        scan_state = {**scan_state, 'status': 'failed', 'message': str(exc), 'finished_at': datetime.now(timezone.utc).isoformat(), 'new_count': 0, 'opportunity_ids': [], 'digest_available': False, 'scheduled': scheduled}


def launch_scan(sources, threshold, source_names, themes, send_email=False, scheduled=False):
    global scan_state
    if not scan_lock.acquire(blocking=False):
        return None
    scan_id = uuid.uuid4().hex[:12]
    scan_state = {'id': scan_id, 'status': 'running', 'message': 'Checking sources and collecting opportunities', 'started_at': datetime.now(timezone.utc).isoformat(), 'finished_at': None, 'new_count': 0, 'opportunity_ids': [], 'digest_available': False, 'dry_run': not send_email, 'scheduled': scheduled}
    def worker():
        try:
            run_scraper(scan_id, sources, threshold, source_names, themes, send_email, scheduled)
        finally:
            scan_lock.release()
    threading.Thread(target=worker, daemon=True).start()
    return scan_state


@app.post('/api/scans', status_code=202)
def start_scan(sources: Literal['rss', 'web', 'all'] = 'all', threshold: int | None = Query(default=None, ge=0, le=100), source_names: list[str] = Query(default=[]), themes: list[str] = Query(default=[]), send_email: bool = False, credentials: HTTPAuthorizationCredentials | None = Depends(admin_bearer)):
    if send_email:
        require_admin(credentials)
    selected_threshold = load_scraper().RELEVANCE_THRESHOLD if threshold is None else threshold
    selected_sources = None if sources == 'all' else sources
    result = launch_scan(selected_sources, selected_threshold, source_names, themes, send_email)
    if result is None:
        raise HTTPException(409, 'A scan is already running')
    return result
@app.get('/api/scans/latest')
def latest_scan():
    return scan_state


@app.get('/api/scans/latest/digest', response_class=HTMLResponse)
def latest_scan_digest(_admin: bool = Depends(require_admin)):
    if scan_state.get('status') != 'completed' or not scan_state.get('digest_available'):
        raise HTTPException(404, 'The latest completed scan has no digest to preview')
    scraper = load_scraper()
    ids = scan_state.get('opportunity_ids', [])
    with connect() as db:
        rows = db.execute('SELECT * FROM opportunities WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids).fetchall()
    records = [scraper.CFPRecord(title=row['title'], url=row['url'], source=row['source'], snippet=row['summary'], source_type=row['source_type'], published=row['published'], deadline=row['deadline'], budget=row['budget'], eligible=row['eligible_regions'], consortium=None if row['consortium'] is None else bool(row['consortium']), themes_matched=json.loads(row['themes']), relevance_score=row['score'], ai_summary=row['summary'], urgency=row['urgency']) for row in rows]
    return scraper.build_email_html(records, datetime.now().strftime('%A, %d %B %Y — %H:%M WAT'))

# Scraper utilities exposed directly for interactive use from the dashboard/API.
@app.get('/api/scraper/settings')
def scraper_settings():
    scraper = load_scraper()
    return {'relevance_threshold': scraper.RELEVANCE_THRESHOLD,
            'high_priority_threshold': scraper.HIGH_PRIORITY_THRESHOLD,
            'email_mode': scraper.EMAIL_MODE,
            'email_configured': bool(scraper.EMAIL_CONFIG.get('password') not in ('YOUR_M365_PASSWORD_OR_APP_PASSWORD', '', None) or scraper.EMAIL_CONFIG.get('graph_client_secret') not in ('YOUR_CLIENT_SECRET', '', None)),
            'bot_name': scraper.BOT_NAME, 'bot_version': scraper.BOT_VERSION,
            'crawl_delay_seconds': scraper.CRAWL_DELAY_SECONDS,
            'request_timeout_seconds': scraper.REQUEST_TIMEOUT,
            'robots_cache_ttl_seconds': scraper.ROBOTS_CACHE_TTL}

@app.patch('/api/scraper/settings')
def update_scraper_settings(payload: dict = Body(...), _admin: bool = Depends(require_admin)):
    scraper = load_scraper()
    if 'relevance_threshold' in payload:
        value = payload['relevance_threshold']
        if not isinstance(value, int) or not 0 <= value <= 100: raise HTTPException(422, 'relevance_threshold must be 0..100')
        scraper.RELEVANCE_THRESHOLD = value
    if 'high_priority_threshold' in payload:
        value = payload['high_priority_threshold']
        if not isinstance(value, int) or not 0 <= value <= 100: raise HTTPException(422, 'high_priority_threshold must be 0..100')
        scraper.HIGH_PRIORITY_THRESHOLD = value
    if 'email_mode' in payload:
        if payload['email_mode'] not in ('smtp', 'apppassword', 'graph'): raise HTTPException(422, 'email_mode must be smtp, apppassword, or graph')
        scraper.EMAIL_MODE = payload['email_mode']
    config = read_runtime_config()
    saved = config.setdefault('settings', {})
    saved.update({'relevance_threshold': scraper.RELEVANCE_THRESHOLD, 'high_priority_threshold': scraper.HIGH_PRIORITY_THRESHOLD, 'email_mode': scraper.EMAIL_MODE})
    write_runtime_config(config)
    return scraper_settings()

@app.get('/api/scraper/config')
def get_scraper_config():
    scraper = load_scraper()
    return {'settings': {'relevance_threshold': scraper.RELEVANCE_THRESHOLD, 'high_priority_threshold': scraper.HIGH_PRIORITY_THRESHOLD, 'crawl_delay_seconds': scraper.CRAWL_DELAY_SECONDS, 'request_timeout_seconds': scraper.REQUEST_TIMEOUT, 'robots_cache_ttl_seconds': scraper.ROBOTS_CACHE_TTL, 'email_mode': scraper.EMAIL_MODE}, 'sources': {'api': scraper.API_SOURCES, 'rss': scraper.RSS_SOURCES, 'web': scraper.WEB_SOURCES}, 'themes': scraper.IITA_THEMES, 'africa_keywords': scraper.AFRICA_KEYWORDS, 'schedule': {key: get_schedule_config()[key] for key in ('enabled', 'time', 'send_email')}, 'email': {'from_addr': scraper.EMAIL_CONFIG['from_addr'], 'recipients': scraper.EMAIL_CONFIG['to_addrs'], 'smtp_host': scraper.EMAIL_CONFIG['smtp_host'], 'smtp_port': scraper.EMAIL_CONFIG['smtp_port'], 'credentials_configured': bool(scraper.EMAIL_CONFIG.get('password') not in ('YOUR_M365_PASSWORD_OR_APP_PASSWORD', '', None) or scraper.EMAIL_CONFIG.get('graph_client_secret') not in ('YOUR_CLIENT_SECRET', '', None))}}

@app.patch('/api/scraper/config')
def patch_scraper_config(payload: dict = Body(...), _admin: bool = Depends(require_admin)):
    config = read_runtime_config()
    saved = config.setdefault('settings', {})
    ranges = {'relevance_threshold': (0, 100, int), 'high_priority_threshold': (0, 100, int), 'request_timeout_seconds': (1, 120, int), 'robots_cache_ttl_seconds': (1, 86400, int), 'crawl_delay_seconds': (0, 60, (int, float))}
    requested = payload.get('settings', {})
    if not isinstance(requested, dict): raise HTTPException(422, 'settings must be an object')
    for key, (minimum, maximum, kind) in ranges.items():
        if key in requested:
            value = requested[key]
            if isinstance(value, bool) or not isinstance(value, kind) or not minimum <= value <= maximum: raise HTTPException(422, f'{key} must be between {minimum} and {maximum}')
            saved[key] = value
    if 'email_mode' in requested:
        if requested['email_mode'] not in ('smtp', 'apppassword', 'graph'): raise HTTPException(422, 'email_mode must be smtp, apppassword, or graph')
        saved['email_mode'] = requested['email_mode']
    if 'themes' in payload:
        themes = payload['themes']
        if not isinstance(themes, dict) or not themes or any(not isinstance(name, str) or not name.strip() or not isinstance(words, list) or not words or any(not isinstance(word, str) or not word.strip() for word in words) for name, words in themes.items()): raise HTTPException(422, 'themes must map names to non-empty keyword arrays')
        config['themes'] = themes
    if 'africa_keywords' in payload:
        words = payload['africa_keywords']
        if not isinstance(words, list) or any(not isinstance(word, str) or not word.strip() for word in words): raise HTTPException(422, 'africa_keywords must be an array of non-empty strings')
        config['africa_keywords'] = words
    if 'sources' in payload:
        sources = payload['sources']
        if not isinstance(sources, dict) or any(group not in ('api', 'rss', 'web') for group in sources): raise HTTPException(422, 'sources must contain only api, rss, or web arrays')
        for group, rows in sources.items():
            if not isinstance(rows, list): raise HTTPException(422, f'sources.{group} must be an array')
            names = set()
            for source in rows:
                if not isinstance(source, dict) or not isinstance(source.get('name'), str) or not source['name'].strip(): raise HTTPException(422, f'Every {group} source requires a name')
                parsed = urlparse(str(source.get('url', '')))
                if parsed.scheme not in ('https', 'http') or not parsed.netloc: raise HTTPException(422, f'Every {group} source requires an absolute HTTP(S) URL')
                if source['name'] in names: raise HTTPException(422, f'Duplicate source name in {group}: {source["name"]}')
                names.add(source['name'])
                if group == 'web' and not isinstance(source.get('selector'), str): raise HTTPException(422, 'Every web source requires a CSS selector')
                if group == 'api' and (source.get('method') not in ('GET', 'POST') or not isinstance(source.get('result_path'), list) or not source.get('title_field') or not source.get('url_template')): raise HTTPException(422, 'Every API source requires GET/POST method, result_path, title_field, and url_template')
        config.setdefault('sources', {}).update(sources)
    if 'schedule' in payload:
        schedule = payload['schedule']
        if not isinstance(schedule, dict) or any(key not in ('enabled', 'time', 'send_email') for key in schedule):
            raise HTTPException(422, 'schedule accepts only enabled, time, and send_email')
        if 'enabled' in schedule and not isinstance(schedule['enabled'], bool):
            raise HTTPException(422, 'schedule.enabled must be a boolean')
        if 'send_email' in schedule and not isinstance(schedule['send_email'], bool):
            raise HTTPException(422, 'schedule.send_email must be a boolean')
        if 'time' in schedule and (not isinstance(schedule['time'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule['time'])):
            raise HTTPException(422, 'schedule.time must use 24-hour HH:MM format')
        config.setdefault('schedule', {}).update(schedule)
    if 'email' in payload:
        email = payload['email']
        if not isinstance(email, dict) or any(key not in ('from_addr', 'to_addrs', 'smtp_host', 'smtp_port', 'username') for key in email): raise HTTPException(422, 'Only sender, recipients, SMTP host/port, and username can be configured here; credentials stay in backend environment variables')
        for key in ('from_addr', 'username'):
            if key in email and (not isinstance(email[key], str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email[key])): raise HTTPException(422, f'{key} must be a valid email address')
        if 'to_addrs' in email and (not isinstance(email['to_addrs'], list) or not email['to_addrs'] or any(not isinstance(address, str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', address) for address in email['to_addrs'])): raise HTTPException(422, 'to_addrs must be a non-empty array of valid email addresses')
        if 'smtp_host' in email and (not isinstance(email['smtp_host'], str) or not email['smtp_host'].strip()): raise HTTPException(422, 'smtp_host must be a non-empty hostname')
        if 'smtp_port' in email and (isinstance(email['smtp_port'], bool) or not isinstance(email['smtp_port'], int) or not 1 <= email['smtp_port'] <= 65535): raise HTTPException(422, 'smtp_port must be between 1 and 65535')
        config.setdefault('email', {}).update(email)
    write_runtime_config(config)
    scraper = load_scraper()
    return get_scraper_config()
@app.post('/api/scraper/prefilter')
def prefilter_text(payload: dict = Body(...)):
    text = payload.get('text')
    if not isinstance(text, str) or not text.strip(): raise HTTPException(422, 'text is required')
    scraper = load_scraper()
    matched, themes = scraper.keyword_prefilter(text)
    return {'matched': matched, 'themes': themes}

@app.post('/api/scraper/score')
def score_candidate(payload: dict = Body(...)):
    from fastapi import HTTPException
    scraper = load_scraper()
    title = str(payload.get('title', '')).strip()
    if not title: raise HTTPException(422, 'title is required')
    themes = payload.get('themes') or []
    if not themes:
        _, themes = scraper.keyword_prefilter(title + ' ' + str(payload.get('snippet', '')))
    record = scraper.CFPRecord(title=title, url=str(payload.get('url', '')), source=str(payload.get('source', 'Manual entry')), snippet=str(payload.get('snippet', '')), source_type=str(payload.get('source_type', 'web')), published=payload.get('published'), themes_matched=themes)
    scored = scraper.score_with_claude(record)
    return {'title': scored.title, 'url': scored.url, 'source': scored.source, 'source_type': scored.source_type, 'published': scored.published, 'deadline': scored.deadline, 'budget': scored.budget, 'eligible_regions': scored.eligible, 'consortium': scored.consortium, 'themes': scored.themes_matched, 'score': scored.relevance_score, 'urgency': scored.urgency, 'summary': scored.ai_summary, 'fingerprint': scored.compute_fingerprint()}

@app.get('/api/robots/audit')
def robots_audit_get():
    return audit_robots()

@app.get('/api/email/config')
def email_configuration():
    scraper = load_scraper()
    return {'mode': scraper.EMAIL_MODE, 'from': scraper.EMAIL_CONFIG['from_addr'], 'recipients': scraper.EMAIL_CONFIG['to_addrs'], 'smtp_host': scraper.EMAIL_CONFIG['smtp_host'], 'smtp_port': scraper.EMAIL_CONFIG['smtp_port'], 'graph_configured': all(scraper.EMAIL_CONFIG.get(k) not in ('YOUR_TENANT_ID','YOUR_CLIENT_ID','YOUR_CLIENT_SECRET') for k in ('graph_tenant_id','graph_client_id','graph_client_secret')), 'smtp_configured': scraper.EMAIL_CONFIG.get('password') not in ('YOUR_M365_PASSWORD_OR_APP_PASSWORD', '', None)}

@app.post('/api/email/preview', response_class=HTMLResponse)
def preview_email(payload: dict = Body(...)):
    scraper = load_scraper()
    ids = payload.get('opportunity_ids') or []
    if not ids: raise HTTPException(422, 'opportunity_ids is required')
    with connect() as db:
        rows = db.execute('SELECT * FROM opportunities WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids).fetchall()
    if len(rows) != len(set(ids)): raise HTTPException(404, 'One or more opportunities were not found')
    records = [scraper.CFPRecord(title=r['title'], url=r['url'], source=r['source'], snippet=r['summary'], source_type=r['source_type'], published=r['published'], deadline=r['deadline'], budget=r['budget'], eligible=r['eligible_regions'], consortium=None if r['consortium'] is None else bool(r['consortium']), themes_matched=json.loads(r['themes']), relevance_score=r['score'], ai_summary=r['summary'], urgency=r['urgency']) for r in rows]
    return scraper.build_email_html(records, datetime.now().strftime('%A, %d %B %Y — %H:%M WAT'))

@app.post('/api/email/send')
def send_digest(payload: dict = Body(...), _admin: bool = Depends(require_admin)):
    scraper = load_scraper()
    ids = payload.get('opportunity_ids') or []
    if not ids: raise HTTPException(422, 'opportunity_ids is required')
    with connect() as db:
        rows = db.execute('SELECT * FROM opportunities WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids).fetchall()
    if len(rows) != len(set(ids)): raise HTTPException(404, 'One or more opportunities were not found')
    records = [scraper.CFPRecord(title=r['title'], url=r['url'], source=r['source'], snippet=r['summary'], source_type=r['source_type'], published=r['published'], deadline=r['deadline'], budget=r['budget'], eligible=r['eligible_regions'], consortium=None if r['consortium'] is None else bool(r['consortium']), themes_matched=json.loads(r['themes']), relevance_score=r['score'], ai_summary=r['summary'], urgency=r['urgency']) for r in rows]
    html = scraper.build_email_html(records, datetime.now().strftime('%A, %d %B %Y — %H:%M WAT'))
    try: scraper.send_email(html, records, dry_run=bool(payload.get('dry_run', False)))
    except Exception as exc: raise HTTPException(502, f'Email delivery failed: {exc}')
    return {'status': 'previewed' if payload.get('dry_run') else 'sent', 'subject': scraper._subject(records), 'recipient_count': len(scraper.EMAIL_CONFIG['to_addrs']), 'opportunity_count': len(records)}

@app.get('/api/scraper/seen')
def seen_records(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    scraper = load_scraper()
    scraper.init_db(scraper.DB_PATH).close()
    with sqlite3.connect(scraper.DB_PATH) as db:
        db.row_factory = sqlite3.Row
        total = db.execute('SELECT COUNT(*) FROM seen_cfps').fetchone()[0]
        rows = db.execute('SELECT * FROM seen_cfps ORDER BY first_seen DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    return {'items': [dict(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset}