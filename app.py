"""app.py — Application entry point."""

import logging
import signal
import sys
import webbrowser
import threading
from logging.handlers import TimedRotatingFileHandler
from models.database import Database
from engine.manager import EngineManager
from web.routes import app, init_app, init_manager, set_encryption_key
from config import DB_PATH, LOG_PATH, HOST, PORT, SERVER_MODE
from utils.net import resolve_port

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        # 按天切,留 30 天:一个文件长到几 GB 就没法排查了(实盘 57MB/天、单文件累到
        # 3.1GB,grep 一次要几分钟)。跨天重启也会补切——rolloverAt 按文件 mtime 算,
        # 不需要程序在午夜时刻活着。
        TimedRotatingFileHandler(
            LOG_PATH, when="midnight", backupCount=30, encoding="utf-8"
        ),
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

    # Windows (Hyper-V/WSL) may reserve the configured port; fall back to a
    # free one so the app still starts, and open the browser to the real port.
    # 服务器模式下不回退 —— 反向代理写死了端口。
    port = resolve_port(HOST, PORT, SERVER_MODE)
    if port != PORT:
        logger.warning("端口 %d 不可用（可能被系统保留），改用 %d", PORT, port)

    if not SERVER_MODE:
        # Open browser after a short delay
        def open_browser():
            import time

            time.sleep(1.5)
            url = f"http://{HOST}:{port}"
            if pw_hash is None:
                url += "/setup"
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()

    if SERVER_MODE:
        from waitress import serve

        logger.info(
            "服务器模式启动:http://%s:%d（对外的 HTTPS 由反向代理提供）", HOST, port
        )
        # 必须单进程:routes 的 db/manager/encryption_key 是模块级全局,
        # 引擎是进程内线程。多 worker 会让同一批钱包被两套引擎重复下单。
        serve(app, host=HOST, port=port, threads=8)
    else:
        logger.info("Starting Polymarket Market Maker on http://%s:%d", HOST, port)
        app.run(host=HOST, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
