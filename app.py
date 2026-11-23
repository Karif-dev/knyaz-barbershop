import os
import json
import base64
import ssl
import http.client
import threading
import time
import logging
import hashlib
import urllib.request
import hmac
import secrets
import fcntl
from io import BytesIO

from flask import Flask, send_from_directory, request, jsonify, session, redirect, url_for, abort

logging.basicConfig(level=logging.INFO, stream=__import__('sys').stdout)
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

# Only explicitly allowed public media is served; no /config.json or /app.py leaks.
app = Flask(__name__, static_folder=None)
_admin_secret = os.environ.get("ADMIN_PASSWORD", "")
_session_seed = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(48)
# The admin password is also part of the session signing key: old exposed
# fallback Flask secrets alone cannot forge admin cookies after this change.
app.secret_key = hmac.new(_session_seed.encode(), ("knyaz-session:" + _admin_secret).encode(), hashlib.sha256).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True)

# ── Config helpers ───────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        # Ignore legacy values even on a container running an old snapshot.
        cfg.pop("admin_password", None)
        return cfg
    return {
        "gallery": [],
        "team": [],
        "service_images": {},
        "services_prices": {"last_sync": None, "services": []}
    }

def save_config(cfg):
    # Never publish admin credentials through config.json or GitHub.
    safe_cfg = {k: v for k, v in cfg.items() if k != "admin_password"}
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(safe_cfg, f, ensure_ascii=False, indent=2)

def push_to_github(filepath, commit_msg=None):
    """Push a single file to GitHub using http.client (proxy-safe)."""
    token = os.environ.get('GITHUB_TOKEN', '')
    repo  = os.environ.get('GITHUB_REPO', 'Karif-dev/knyaz-barbershop')
    branch = os.environ.get('GITHUB_BRANCH', 'main')

    if not token:
        log.warning("GITHUB_TOKEN not set — skipping GitHub push")
        return False

    rel_path = os.path.relpath(filepath, BASE_DIR).replace(os.sep, '/')
    if not commit_msg:
        commit_msg = f"admin: update {rel_path}"

    ctx = ssl.create_default_context()

    # GET current SHA
    try:
        conn = http.client.HTTPSConnection("api.github.com", context=ctx)
        conn.request("GET", f"/repos/{repo}/contents/{rel_path}",
            headers={"Authorization": f"token {token}",
                     "User-Agent": "knyaz-push/1.0",
                     "Accept": "application/vnd.github.v3+json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        sha = data.get('sha')
    except Exception as e:
        log.warning("GitHub GET failed: %s", e)
        sha = None

    with open(filepath, 'rb') as f:
        content_b64 = base64.b64encode(f.read()).decode()

    payload = {"message": commit_msg, "content": content_b64, "branch": branch}
    if sha:
        payload["sha"] = sha

    try:
        conn2 = http.client.HTTPSConnection("api.github.com", context=ctx)
        conn2.request("PUT", f"/repos/{repo}/contents/{rel_path}",
            body=json.dumps(payload).encode(),
            headers={"Authorization": f"token {token}",
                     "User-Agent": "knyaz-push/1.0",
                     "Accept": "application/vnd.github.v3+json",
                     "Content-Type": "application/json"})
        status = conn2.getresponse().status
        log.info("GitHub push %s → %s", rel_path, status)
        return status in (200, 201)
    except Exception as e:
        log.warning("GitHub PUT failed: %s", e)
        return False

def _admin_password():
    value = os.environ.get("ADMIN_PASSWORD", "")
    # A leaked historical password must never be an implicit fallback.
    return value if len(value) >= 4 else None


def _password_valid(given):
    expected = _admin_password()
    return bool(expected and isinstance(given, str) and hmac.compare_digest(given, expected))


# A four-character password is weak on a public site, so limit online guesses.
# This lock is shared by HTTP login endpoints in the single Gunicorn worker.
_AUTH_FAILURES = {}
_AUTH_LOCK = threading.Lock()
_AUTH_WINDOW = 15 * 60
_AUTH_MAX_FAILURES = 5

def _auth_client_key():
    return request.remote_addr or "unknown"

def _auth_is_blocked():
    key = _auth_client_key()
    now = time.monotonic()
    with _AUTH_LOCK:
        attempts = [t for t in _AUTH_FAILURES.get(key, []) if now - t < _AUTH_WINDOW]
        if attempts:
            _AUTH_FAILURES[key] = attempts
        else:
            _AUTH_FAILURES.pop(key, None)
        return len(attempts) >= _AUTH_MAX_FAILURES

def _auth_record_failure():
    key = _auth_client_key()
    now = time.monotonic()
    with _AUTH_LOCK:
        _AUTH_FAILURES[key] = [t for t in _AUTH_FAILURES.get(key, [])
                                if now - t < _AUTH_WINDOW] + [now]
        # Keep memory bounded if bots send many distinct addresses.
        if len(_AUTH_FAILURES) > 4000:
            for old_key in list(_AUTH_FAILURES)[:1000]:
                _AUTH_FAILURES.pop(old_key, None)

def _auth_clear_failures():
    with _AUTH_LOCK:
        _AUTH_FAILURES.pop(_auth_client_key(), None)

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _admin_password():
            return jsonify({'ok': False, 'error': 'ADMIN_PASSWORD не настроен в Railway (не менее 4 символов)'}), 503
        if not session.get('admin_logged_in'):
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return decorated

# ── Public routes ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/api/config')
def api_config():
    cfg = load_config()
    # Never expose password
    safe = {k: v for k, v in cfg.items() if k != 'admin_password'}
    return jsonify(safe)


PUBLIC_MEDIA_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".ico"}

@app.route('/<path:filename>')
def public_media(filename):
    # All site's local media live at repository root. Block all nested paths,
    # Python code, config files, HTML admin pages, Git files and source maps.
    if '/' in filename or filename.startswith('.') or os.path.splitext(filename)[1].lower() not in PUBLIC_MEDIA_TYPES:
        abort(404)
    return send_from_directory(BASE_DIR, filename)

# ── Admin auth ───────────────────────────────────────────────────────────────

@app.route('/admin')
@app.route('/admin/')
@admin_required
def admin_panel():
    return send_from_directory(BASE_DIR, 'admin.html')

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if not _admin_password():
            return "ADMIN_PASSWORD не настроен в Railway", 503
        if _auth_is_blocked():
            return "Слишком много попыток. Повтори через 15 минут.", 429
        if _password_valid(request.form.get('password', '')):
            _auth_clear_failures()
            session.clear()
            session['admin_logged_in'] = True
            return redirect('/admin')
        _auth_record_failure()
        return send_from_directory(BASE_DIR, 'admin_login.html'), 401
    return send_from_directory(BASE_DIR, 'admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect('/admin/login')

@app.route('/admin/auth', methods=['POST'])
def admin_auth():
    if not _admin_password():
        return jsonify({'ok': False, 'error': 'ADMIN_PASSWORD не настроен в Railway'}), 503
    if _auth_is_blocked():
        return jsonify({'ok': False, 'error': 'Слишком много попыток. Повтори через 15 минут.'}), 429
    data = request.get_json(silent=True) or {}
    if _password_valid(data.get('password', '')):
        _auth_clear_failures()
        session.clear()
        session['admin_logged_in'] = True
        return jsonify({'ok': True})
    _auth_record_failure()
    return jsonify({'ok': False, 'error': 'Неверный пароль'}), 401

# ── Admin API ────────────────────────────────────────────────────────────────

@app.route('/admin/api/config', methods=['GET'])
@admin_required
def admin_get_config():
    return jsonify(load_config())

@app.route('/admin/api/save', methods=['POST'])
@admin_required
def admin_save():
    """Save non-secret settings only; credentials live in Railway env."""
    data = request.get_json()
    if not data:
        return jsonify({'ok': False, 'error': 'no data'}), 400
    if data.get('new_password'):
        return jsonify({'ok': False, 'error': 'Пароль теперь меняется через ADMIN_PASSWORD в Railway, не через GitHub'}), 400

    cfg = load_config()

    if 'gallery' in data:
        cfg['gallery'] = data['gallery']
    if 'team' in data:
        cfg['team'] = data['team']
    if 'service_images' in data:
        cfg['service_images'] = data['service_images']
    save_config(cfg)
    ok = push_to_github(CONFIG_FILE, "admin: update config")
    return jsonify({'ok': True, 'pushed': ok})

@app.route('/admin/api/upload', methods=['POST'])
@admin_required
def admin_upload():
    """Upload an image. Returns the URL path."""
    file = request.files.get('file')
    if not file:
        return jsonify({'ok': False, 'error': 'no file'}), 400

    # Validate type
    allowed = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
    if file.content_type not in allowed:
        return jsonify({'ok': False, 'error': 'Только JPG / PNG / WebP'}), 400

    # Generate safe filename
    import uuid
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
    filename = f"upload_{uuid.uuid4().hex[:8]}.{ext}"
    dest = os.path.join(BASE_DIR, filename)
    file.save(dest)

    # Push to GitHub
    push_to_github(dest, f"admin: upload image {filename}")

    return jsonify({'ok': True, 'url': f'/{filename}', 'filename': filename})

def _fetch_yclients_services():
    """
    Fetch services + prices from the YClients booking SPA.
    Primary: Playwright headless Chromium renders JS and intercepts API responses.
    Fallback: plain HTTP (works only if page has SSR/inline JSON).
    Target URL: https://n2527773.yclients.ru/company/2188101/personal/select-services?o=m-1
    """
    import re
    import json as _json
    ctx = ssl.create_default_context()
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    TARGET_URL = "https://n2527773.yclients.ru/company/2188101/personal/select-services?o=m-1"

    def _dedupe(services):
        seen = set(); out = []
        for s in services:
            if s['name'] not in seen:
                seen.add(s['name']); out.append(s)
        return out

    def _decode_ue(s):
        """Decode JSON unicode escapes \\u041c -> М inside a captured string."""
        return re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)

    def _has_cyrillic(s):
        """Return True if string contains at least one Cyrillic character."""
        return any('Ѐ' <= c <= 'ӿ' for c in s)

    PRICE_KEYS = {'price_min', 'price_max', 'price', 'cost'}

    def _has_price_key(item):
        return isinstance(item, dict) and bool(PRICE_KEYS & set(item.keys()))

    def _extract_rows(data, depth=0):
        """Recursively find a list of service dicts that have price fields."""
        if depth > 6:
            return []
        if isinstance(data, list) and data:
            # Must have title/name AND at least one item with a price key
            sample = data[:5]
            has_name = any(isinstance(i, dict) and ('title' in i or 'name' in i) for i in sample)
            has_price = any(_has_price_key(i) for i in sample)
            if has_name and has_price:
                return data
        if isinstance(data, dict):
            # Prioritised keys
            for key in ('data', 'services', 'items', 'result', 'records'):
                v = data.get(key)
                if v is not None:
                    rows = _extract_rows(v, depth+1)
                    if rows:
                        return rows
            # Fallback: any value
            for v in data.values():
                rows = _extract_rows(v, depth+1)
                if rows:
                    return rows
        return []

    def _parse_content(raw):
        """Extract {name, price} services from JSON or HTML string."""
        services = []
        try:
            data = _json.loads(raw)
            rows = _extract_rows(data)
            for item in rows:
                if not isinstance(item, dict):
                    continue
                name = (item.get('title') or item.get('name') or '').strip()
                if not name:
                    continue
                # Skip UI strings that have no Cyrillic (language switchers, "English", etc.)
                if not _has_cyrillic(name):
                    continue
                # price_min=0 means price range — fall back to price_max, then price, then cost
                price = int(item.get('price_min') or 0)
                if price == 0:
                    price = int(item.get('price_max') or item.get('price') or item.get('cost') or 0)
                # Only keep items that have a real price — filters out page UI elements
                # (language switchers, category headers, shop names, etc. have no price)
                if price > 0:
                    services.append({"name": name, "price": price})
            if services:
                return services
        except Exception:
            pass

        # Regex fallback: "title":"NAME" near price fields within 600 chars
        # NOTE: raw JSON may contain \\uXXXX escapes — decode them after capture
        price_pats = [r'"price_min"\s*:\s*(\d+)', r'"price_max"\s*:\s*(\d+)',
                      r'"price"\s*:\s*(\d+)', r'"cost"\s*:\s*(\d+)']
        for m in re.finditer(r'"title"\s*:\s*"([^"\\]{2,100}(?:\\.[^"\\]{0,100})*)"', raw):
            raw_name = m.group(1)
            name = _decode_ue(raw_name)
            if not _has_cyrillic(name):
                continue
            tail = raw[m.start():m.start()+600]
            price = 0
            for pp in price_pats:
                pm = re.search(pp, tail)
                if pm:
                    v = int(pm.group(1))
                    if v > 0:
                        price = v
                        break
            if price > 0:
                services.append({"name": name, "price": price})
        if services:
            return services
        # Reverse scan
        for pp in price_pats:
            for m in re.finditer(pp, raw):
                chunk = raw[max(0, m.start()-600):m.start()+100]
                tm = re.search(r'"title"\s*:\s*"([^"\\]{2,100})"', chunk)
                if tm:
                    name = _decode_ue(tm.group(1))
                    if _has_cyrillic(name) and int(m.group(1)) > 0:
                        services.append({"name": name, "price": int(m.group(1))})
        return services

    # Strategy 1: Playwright headless browser (installed at build time via nixpacks)
    try:
        from playwright.sync_api import sync_playwright
        log.info("YClients: launching Playwright for %s", TARGET_URL)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=[
                '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
            ])
            context = browser.new_context(user_agent=ua, locale='ru-RU')
            page = context.new_page()

            # Intercept API JSON responses (fastest path — catches data before DOM render)
            captured = []
            def on_response(resp):
                try:
                    if resp.status == 200 and 'yclients' in resp.url:
                        ct = resp.headers.get('content-type', '')
                        url = resp.url
                        # Only parse responses likely to contain service data
                        if 'json' in ct and any(k in url for k in (
                            'service', 'book', 'price', 'staff', 'category'
                        )):
                            svcs = _parse_content(resp.text())
                            if svcs:
                                captured.extend(svcs)
                except Exception:
                    pass
            page.on('response', on_response)

            page.goto(TARGET_URL, wait_until='networkidle', timeout=60000)
            page.wait_for_timeout(3000)  # extra wait for Vue render

            if not captured:
                captured = _parse_content(page.content())

            browser.close()

        if captured:
            log.info("YClients Playwright: got %d services", len(captured))
            return _dedupe(captured)
        log.warning("YClients Playwright: page loaded but no services found in DOM/API")
    except Exception as e:
        log.warning("YClients Playwright failed: %s", e)

    # Strategy 2: plain HTTP fallback
    def _fetch_url(url, accept='text/html'):
        req = urllib.request.Request(url, headers={
            "User-Agent": ua, "Accept": accept,
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": "https://n2527773.yclients.ru/",
        })
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return resp.read().decode('utf-8', errors='ignore')

    for url in [TARGET_URL, "https://n2527773.yclients.ru/company/2188101/"]:
        try:
            html = _fetch_url(url)
            log.info("YClients HTTP: %d bytes from %s", len(html), url)
            svcs = _parse_content(html)
            if svcs:
                log.info("YClients HTTP: got %d services", len(svcs))
                return _dedupe(svcs)
        except Exception as e:
            log.debug("YClients HTTP %s: %s", url, e)

    # Strategy 3: public JSON API
    for url in [
        "https://api.yclients.com/api/v1/company/2188101/services/?count=200&active=1",
        "https://api.yclients.com/api/v1/book_services/2527773/?show_all=1",
    ]:
        try:
            raw = _fetch_url(url, accept='application/json')
            svcs = _parse_content(raw)
            if svcs:
                log.info("YClients API: got %d services from %s", len(svcs), url)
                return _dedupe(svcs)
        except Exception as e:
            log.debug("YClients API %s: %s", url, e)

    log.warning("YClients: all strategies exhausted — no services found")
    return []


def _services_signature(services):
    """Compare only meaningful prices and names, ignoring order and sync timestamps."""
    return sorted((str(s.get('name', '')).strip(), int(s.get('price', 0)))
                  for s in services if s.get('name'))


def _persist_yclients_services(services, commit_message):
    cfg = load_config()
    if _services_signature(cfg.get('services_prices', {}).get('services', [])) == _services_signature(services):
        log.info("YClients: unchanged (%d services), no commit or redeploy", len(services))
        return False
    cfg['services_prices'] = {
        "last_sync": time.strftime("%Y-%m-%d %H:%M"),
        "services": services,
    }
    save_config(cfg)
    push_to_github(CONFIG_FILE, commit_message)
    return True


@app.route('/admin/api/sync-yclients', methods=['POST'])
@admin_required
def admin_sync_yclients():
    """Sync services & prices from YClients."""
    try:
        services = _fetch_yclients_services()
        if not services:
            return jsonify({
                'ok': False,
                'error': (
                    'YClients не вернул данные — виджет использует JavaScript-рендеринг, '
                    'и серверный запрос не получает цены. '
                    'Введите прайс-лист вручную в таблице ниже и нажмите «Сохранить прайс».'
                )
            })

        changed = _persist_yclients_services(services, "auto: sync yclients prices")
        return jsonify({'ok': True, 'services': services, 'count': len(services), 'changed': changed})
    except Exception as e:
        log.error("YClients sync error: %s", e)
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/admin/api/save-prices', methods=['POST'])
@admin_required
def admin_save_prices():
    """Manually save price list (name + price pairs)."""
    data = request.get_json() or {}
    services = data.get('services', [])
    if not isinstance(services, list):
        return jsonify({'ok': False, 'error': 'bad data'}), 400

    # Validate and clean
    clean = []
    for s in services:
        name = str(s.get('name', '')).strip()
        try:
            price = int(s.get('price', 0))
        except (ValueError, TypeError):
            price = 0
        if name and price > 0:
            clean.append({"name": name, "price": price})

    if not clean:
        return jsonify({'ok': False, 'error': 'Нет данных для сохранения'}), 400

    changed = _persist_yclients_services(clean, "admin: manual price list update")
    return jsonify({'ok': True, 'count': len(clean), 'changed': changed})


# ── Background updaters ───────────────────────────────────────────────────────

# Background refresh uses one single Yandex browser visit per day.
# On CAPTCHA/network errors, do not overwrite the last published cards or counts.
def _sync_reviews():
    import update_reviews
    res = update_reviews.sync()
    if res.get("ok") and res.get("changed"):
        res["pushed"] = push_to_github(
            os.path.join(BASE_DIR, "index.html"), "auto: refresh yandex review cards")
        if not res["pushed"]:
            log.warning("Yandex data changed locally, but GitHub push failed")
    else:
        res["pushed"] = False
    return res


@app.route('/admin/api/sync-reviews', methods=['POST'])
@admin_required
def admin_sync_reviews():
    """Manual one-off sync; no retry loop on CAPTCHA."""
    try:
        return jsonify(_sync_reviews())
    except Exception as e:
        log.error("Reviews sync error: %s", e)
        return jsonify({"ok": False, "error": type(e).__name__}), 502


def _reviews_loop():
    """Refresh at 05:35 Moscow time, after nightly YClients service updates."""
    import datetime
    moscow = datetime.timezone(datetime.timedelta(hours=3))
    while True:
        now = datetime.datetime.now(moscow)
        next_run = now.replace(hour=5, minute=35, second=0, microsecond=0)
        if next_run <= now:
            next_run += datetime.timedelta(days=1)
        delay = max(1, (next_run - now).total_seconds())
        log.info("Yandex reviews next automatic refresh in %.0f min", delay / 60)
        time.sleep(delay)
        try:
            res = _sync_reviews()
            if res.get("ok"):
                log.info("Yandex review refresh: %s", res)
            else:
                log.warning("Yandex review refresh skipped: %s", res.get("error"))
        except Exception as exc:
            log.warning("Yandex review refresh crashed: %s", type(exc).__name__)


def _yclients_loop():
    """Auto-sync YClients services + prices once a day in 00:00–01:00 MSK window."""
    import datetime, random
    MSK_OFFSET = 3  # UTC+3

    while True:
        now_utc = datetime.datetime.utcnow()
        now_msk = now_utc + datetime.timedelta(hours=MSK_OFFSET)

        if now_msk.hour == 0:
            # Already inside the midnight window — run after small jitter
            jitter = random.randint(0, 300)
            time.sleep(jitter)
        else:
            # Sleep until next midnight MSK + random jitter (0–55 min)
            next_midnight = (now_msk + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            wait = (next_midnight - now_msk).total_seconds() + random.randint(0, 3300)
            log.info("YClients auto-sync: next run in %.0f min (MSK midnight window)",
                     wait / 60)
            time.sleep(wait)

        try:
            services = _fetch_yclients_services()
            if services:
                changed = _persist_yclients_services(services, "auto: sync yclients prices+services")
                log.info("YClients auto-sync: %d services checked, changed=%s", len(services), changed)
            else:
                log.warning("YClients auto-sync: no services found")
        except Exception as e:
            log.warning("YClients auto-sync failed: %s", e)

        # Sleep 23h before re-checking the window (avoids double-run in same night)
        time.sleep(23 * 60 * 60)

_background_lock_fd = None

def _start_background_updaters():
    """One scheduler per host even if gunicorn runs multiple worker processes."""
    global _background_lock_fd
    if os.environ.get('DISABLE_BACKGROUND_SYNC') == '1':
        return
    fd = os.open('/tmp/knyaz-background-updaters.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        log.info("Background sync already owned by another worker")
        return
    _background_lock_fd = fd  # keep lock for the lifetime of this worker
    threading.Thread(target=_yclients_loop, daemon=True, name="yclients-sync").start()
    threading.Thread(target=_reviews_loop, daemon=True, name="reviews-updater").start()

_start_background_updaters()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
