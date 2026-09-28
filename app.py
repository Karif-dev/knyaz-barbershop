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
from io import BytesIO

from flask import Flask, send_from_directory, request, jsonify, session, redirect, url_for, abort

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')
app.secret_key = os.environ.get('SECRET_KEY', 'knyaz-barbershop-secret-key-2024')

# ── Config helpers ───────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        "admin_password": "knyaz2024",
        "gallery": [],
        "team": [],
        "service_images": {},
        "services_prices": {"last_sync": None, "services": []}
    }

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

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

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
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

# ── Admin auth ───────────────────────────────────────────────────────────────

@app.route('/admin')
@app.route('/admin/')
@admin_required
def admin_panel():
    return send_from_directory(BASE_DIR, 'admin.html')

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        pwd = request.form.get('password', '')
        cfg = load_config()
        if pwd == cfg.get('admin_password', 'knyaz2024'):
            session['admin_logged_in'] = True
            return redirect('/admin')
        return send_from_directory(BASE_DIR, 'admin.html')  # handled by admin.html JS
    return send_from_directory(BASE_DIR, 'admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect('/admin/login')

@app.route('/admin/auth', methods=['POST'])
def admin_auth():
    data = request.get_json() or {}
    pwd = data.get('password', '')
    cfg = load_config()
    if pwd == cfg.get('admin_password', 'knyaz2024'):
        session['admin_logged_in'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Неверный пароль'}), 401

# ── Admin API ────────────────────────────────────────────────────────────────

@app.route('/admin/api/config', methods=['GET'])
@admin_required
def admin_get_config():
    return jsonify(load_config())

@app.route('/admin/api/save', methods=['POST'])
@admin_required
def admin_save():
    """Save full config (gallery order, team, service images, password)."""
    data = request.get_json()
    if not data:
        return jsonify({'ok': False, 'error': 'no data'}), 400

    cfg = load_config()

    if 'gallery' in data:
        cfg['gallery'] = data['gallery']
    if 'team' in data:
        cfg['team'] = data['team']
    if 'service_images' in data:
        cfg['service_images'] = data['service_images']
    if 'new_password' in data and data['new_password']:
        cfg['admin_password'] = data['new_password']

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
    """Try multiple strategies to get services+prices from YClients."""
    import re
    import json as _json
    ctx = ssl.create_default_context()
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

    # Strategy 1: YClients public booking API (returns JSON directly)
    api_urls = [
        "https://api.yclients.com/api/v1/book_services/2527773/?show_all=1",
        "https://api.yclients.com/api/v1/book_services/2527773/",
    ]
    for api_url in api_urls:
        try:
            req = urllib.request.Request(api_url, headers={
                "User-Agent": ua,
                "Accept": "application/json",
                "Accept-Language": "ru-RU,ru;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                data = _json.loads(resp.read().decode('utf-8', errors='ignore'))
            # Response shape: {"success": true, "data": [...]} or list
            rows = data.get('data', data) if isinstance(data, dict) else data
            services = []
            if isinstance(rows, list):
                for item in rows:
                    if isinstance(item, dict):
                        name = item.get('title') or item.get('name') or ''
                        price = item.get('price_min') or item.get('price') or 0
                        if name and price:
                            services.append({"name": name, "price": int(price)})
            if services:
                return services
        except Exception as e:
            log.debug("YClients API %s failed: %s", api_url, e)

    # Strategy 2: scrape the category/service selection page (static-ish HTML)
    scrape_urls = [
        "https://n2527773.yclients.ru/company/2188101/select-service?iframe=1&lang=ru-RU",
        "https://n2527773.yclients.ru/company/2188101/menu?lang=ru-RU",
    ]
    for url in scrape_urls:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                html = resp.read().decode('utf-8', errors='ignore')

            services = []
            # Pattern 1: "title":"...", ... "price_min":NNN
            for m in re.finditer(r'"title"\s*:\s*"([^"]{2,80})"', html):
                # look for price_min nearby (within 300 chars)
                tail = html[m.start():m.start()+300]
                pm = re.search(r'"price_min"\s*:\s*(\d+)', tail)
                if pm and int(pm.group(1)) > 0:
                    services.append({"name": m.group(1), "price": int(pm.group(1))})

            # Pattern 2: "price_min":NNN, ... "title":"..."
            if not services:
                for m in re.finditer(r'"price_min"\s*:\s*(\d+)', html):
                    if int(m.group(1)) == 0:
                        continue
                    tail = html[max(0, m.start()-300):m.start()+300]
                    tm = re.search(r'"title"\s*:\s*"([^"]{2,80})"', tail)
                    if tm:
                        services.append({"name": tm.group(1), "price": int(m.group(1))})

            if services:
                # Deduplicate by name
                seen = set()
                uniq = []
                for s in services:
                    if s['name'] not in seen:
                        seen.add(s['name'])
                        uniq.append(s)
                return uniq
        except Exception as e:
            log.debug("YClients scrape %s failed: %s", url, e)

    return []


@app.route('/admin/api/sync-yclients', methods=['POST'])
@admin_required
def admin_sync_yclients():
    """Sync services & prices from YClients."""
    try:
        services = _fetch_yclients_services()
        if not services:
            return jsonify({'ok': False, 'error': 'Не удалось получить данные с YClients. '
                            'Возможно, сервис временно недоступен. Попробуйте позже.'})

        cfg = load_config()
        cfg['services_prices'] = {
            "last_sync": time.strftime("%Y-%m-%d %H:%M"),
            "services": services
        }
        save_config(cfg)
        push_to_github(CONFIG_FILE, "auto: sync yclients prices")
        return jsonify({'ok': True, 'services': services, 'count': len(services)})
    except Exception as e:
        log.error("YClients sync error: %s", e)
        return jsonify({'ok': False, 'error': str(e)})

# ── Service image download (startup) ─────────────────────────────────────────

_SERVICE_IMAGES = [
    ("svc1.jpg", "https://images.unsplash.com/photo-1503951914875-452162b0f3f1?w=600&q=75&auto=format&fit=crop"),
    ("svc2.jpg", "https://images.unsplash.com/photo-1599351431202-1e0f0137899a?w=600&q=75&auto=format&fit=crop"),
    ("svc3.jpg", "https://images.unsplash.com/photo-1621605815971-fbc98d665033?w=600&q=75&auto=format&fit=crop"),
    ("svc4.jpg", "https://images.unsplash.com/photo-1622286342621-4bd786c2447c?w=600&q=75&auto=format&fit=crop"),
    ("svc5.jpg", "https://images.unsplash.com/photo-1493256338651-d82f7acb2b38?w=600&q=75&auto=format&fit=crop"),
    # Gallery work photos (barbershop cuts, beards, styling — different from service card images)
    ("gal1.jpg", "https://images.unsplash.com/photo-1605497788044-5a32c7078486?w=800&q=80&auto=format&fit=crop"),
    ("gal2.jpg", "https://images.unsplash.com/photo-1534297635766-a262cdcb8ee4?w=800&q=80&auto=format&fit=crop"),
    ("gal3.jpg", "https://images.unsplash.com/photo-1517832606299-7ae9b720a186?w=800&q=80&auto=format&fit=crop"),
    ("gal4.jpg", "https://images.unsplash.com/photo-1580518337843-f959e992563b?w=800&q=80&auto=format&fit=crop"),
    ("gal5.jpg", "https://images.unsplash.com/photo-1553521041-e56f0ce9c0b4?w=800&q=80&auto=format&fit=crop"),
]

def _download_assets():
    hdrs = {"User-Agent": "Mozilla/5.0 (compatible; knyaz-barbershop/1.0)"}
    for fname, url in _SERVICE_IMAGES:
        dest = os.path.join(BASE_DIR, fname)
        if os.path.exists(dest):
            continue
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as resp:
                with open(dest, "wb") as f:
                    f.write(resp.read())
            log.info("Downloaded %s", fname)
        except Exception as exc:
            log.warning("Could not download %s: %s", fname, exc)

threading.Thread(target=_download_assets, daemon=True, name="asset-dl").start()

# ── Background updaters ───────────────────────────────────────────────────────

def _reviews_loop():
    time.sleep(10)
    while True:
        try:
            import update_reviews
            update_reviews.main()
        except Exception as exc:
            log.warning("Reviews update failed: %s", exc)
        time.sleep(24 * 60 * 60)

def _yclients_loop():
    """Auto-sync YClients prices every 24h."""
    time.sleep(60)  # wait 1 min after startup
    while True:
        try:
            services = _fetch_yclients_services()
            if services:
                cfg = load_config()
                cfg['services_prices'] = {
                    "last_sync": time.strftime("%Y-%m-%d %H:%M"),
                    "services": services
                }
                save_config(cfg)
                log.info("YClients auto-sync: %d services updated", len(services))
            else:
                log.warning("YClients auto-sync: no services found")
        except Exception as e:
            log.warning("YClients auto-sync failed: %s", e)
        time.sleep(24 * 60 * 60)

threading.Thread(target=_reviews_loop, daemon=True, name="reviews-updater").start()
threading.Thread(target=_yclients_loop, daemon=True, name="yclients-sync").start()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
