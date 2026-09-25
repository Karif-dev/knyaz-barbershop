import os
import threading
import time
import logging

from flask import Flask, send_from_directory

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Serve everything (index.html, logo-k.png, etc.) from the project root
app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')


@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


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
