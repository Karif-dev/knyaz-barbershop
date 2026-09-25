import os
import threading
import time
import logging
import urllib.request

from flask import Flask, send_from_directory

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Serve everything (index.html, logo-k.png, etc.) from the project root
app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')


@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


# ── Download service images on first start ──────────────────────────────────

_SERVICE_IMAGES = [
    ("svc1.jpg", "https://images.unsplash.com/photo-1503951914875-452162b0f3f1?w=600&q=75&auto=format&fit=crop"),
    ("svc2.jpg", "https://images.unsplash.com/photo-1599351431202-1e0f0137899a?w=600&q=75&auto=format&fit=crop"),
    ("svc3.jpg", "https://images.unsplash.com/photo-1621605815971-fbc98d665033?w=600&q=75&auto=format&fit=crop"),
    ("svc4.jpg", "https://images.unsplash.com/photo-1622286342621-4bd786c2447c?w=600&q=75&auto=format&fit=crop"),
    ("svc5.jpg", "https://images.unsplash.com/photo-1493256338651-d82f7acb2b38?w=600&q=75&auto=format&fit=crop"),
]

def _download_assets():
    """Download service card images if not present (runs once at startup)."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; knyaz-barbershop/1.0)"}
    for fname, url in _SERVICE_IMAGES:
        dest = os.path.join(BASE_DIR, fname)
        if os.path.exists(dest):
            continue
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                with open(dest, "wb") as f:
                    f.write(resp.read())
            log.info("Downloaded %s (%d bytes)", fname, os.path.getsize(dest))
        except Exception as exc:
            log.warning("Could not download %s: %s", fname, exc)

_asset_thread = threading.Thread(target=_download_assets, daemon=True, name="asset-downloader")
_asset_thread.start()


# ── Background reviews updater ──────────────────────────────────────────────

def _update_loop():
    """Runs update_reviews.main() on startup (after 10s delay) then every 24h."""
    time.sleep(10)
    while True:
        try:
            import update_reviews
            update_reviews.main()
            log.info("Yandex reviews updated successfully.")
        except Exception as exc:
            log.warning("Reviews update failed: %s", exc)
        time.sleep(24 * 60 * 60)   # 24 hours


_updater = threading.Thread(target=_update_loop, daemon=True, name="reviews-updater")
_updater.start()


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
