"""app.py — Application entry point."""

import logging
import signal
import sys
import webbrowser
import threading
from models.database import Database
from engine.manager import EngineManager
from web.routes import app, init_app, init_manager, set_encryption_key
from config import DB_PATH, LOG_PATH, HOST, PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

db = Database(DB_PATH)
manager = None


def on_shutdown(signum=None, frame=None):
    """Graceful shutdown: stop engines, close DB."""
    logger.info("Shutting down...")
    if manager:
        manager.stop_all()
    db.close()
    logger.info("Shutdown complete.")
    sys.exit(0)


def main():
    global manager

    db.init()
    init_app(db)

    # Check if password is set; if so, we can't auto-start engines
    # (user must log in first to provide the password for decryption)
    pw_hash, _ = db.get_password()

    # Register shutdown handler
    signal.signal(signal.SIGINT, on_shutdown)
    signal.signal(signal.SIGTERM, on_shutdown)

    # Open browser after a short delay
    def open_browser():
        import time

        time.sleep(1.5)
        url = f"http://{HOST}:{PORT}"
        if pw_hash is None:
            url += "/setup"
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    logger.info("Starting Polymarket Market Maker on http://%s:%d", HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
