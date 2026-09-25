import os
import threading
import time
import logging

from flask import Flask, send_from_directory

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(BASE_DIR, filename)


# ── Background reviews updater ──────────────────────────────────────────────

def _update_loop():
    """Runs update_reviews.main() on startup and then every 5 hours."""
    # Small delay so the server is fully up before the first fetch
    time.sleep(10)
    while True:
        try:
            import update_reviews
            update_reviews.main()
            log.info("Yandex reviews updated successfully.")
        except Exception as exc:
            log.warning("Reviews update failed: %s", exc)
        time.sleep(5 * 60 * 60)   # 5 hours


_updater = threading.Thread(target=_update_loop, daemon=True, name="reviews-updater")
_updater.start()


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
