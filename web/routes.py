"""web/routes.py — Flask routes and API endpoints."""

import os
import hashlib
import logging
from functools import wraps
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    flash,
)
from models.database import Database
from engine.manager import EngineManager
from utils.crypto import derive_key, encrypt
from config import DB_PATH, HOST, PORT

logger = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)
app.secret_key = os.urandom(32)

db: Database = None
manager: EngineManager = None
encryption_key: bytes = None
_api_cache: dict = {}  # Cache PolymarketAPI instances by address


def init_app(database: Database):
    global db
    db = database


def init_manager(mgr: EngineManager):
    global manager
    manager = mgr


def set_encryption_key(key: bytes):
    global encryption_key
    encryption_key = key


def _get_cached_api(address: str, encrypted_key: str, funder: str = ""):
    """Get or create a cached PolymarketAPI instance for balance queries."""
    if address in _api_cache:
        return _api_cache[address]
    from utils.crypto import decrypt
    from api.polymarket_api import PolymarketAPI

    pk = decrypt(encrypted_key, encryption_key)
    api = PolymarketAPI(pk, funder=funder or None)
    _api_cache[address] = api
    return api


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


# --- Auth Pages ---


@app.route("/setup", methods=["GET", "POST"])
def setup():
    pw_hash, _ = db.get_password()
    if pw_hash is not None:
        return redirect(url_for("login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 6:
            flash("密码至少6个字符")
            return render_template("setup.html")
        if password != confirm:
            flash("两次输入的密码不一致")
            return render_template("setup.html")
        salt = os.urandom(16)
        key = derive_key(password, salt)
        hashed = hashlib.sha256(key).hexdigest()
        db.save_password(hashed, salt)
        set_encryption_key(key)
        # Create manager after setup
        global manager
        mgr = EngineManager(db, key)
        init_manager(mgr)
        session["logged_in"] = True
        return redirect(url_for("dashboard"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    pw_hash, salt = db.get_password()
    if pw_hash is None:
        return redirect(url_for("setup"))
    if request.method == "POST":
        password = request.form.get("password", "")
        key = derive_key(password, salt)
        hashed = hashlib.sha256(key).hexdigest()
        if hashed == pw_hash:
            set_encryption_key(key)
            global manager
            if manager is None:
                mgr = EngineManager(db, key)
                init_manager(mgr)
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("密码错误")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Pages ---


@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/config")
@login_required
def config_page():
    return render_template("config.html")


@app.route("/orders")
@login_required
def orders_page():
    return render_template("orders.html")


@app.route("/history")
@login_required
def history_page():
    return render_template("history.html")


# --- API: Settings ---


@app.route("/api/settings", methods=["GET"])
@login_required
def api_get_settings():
    return jsonify(db.get_settings())


@app.route("/api/settings", methods=["POST"])
@login_required
def api_save_settings():
    data = request.get_json()
    db.save_settings(data)
    return jsonify(
        {
            "ok": True,
            "message": "参数已保存。如需立即生效，请重启引擎；否则将在下次启动时生效。",
        }
    )


# --- API: Wallets ---


@app.route("/api/wallets", methods=["GET"])
@login_required
def api_list_wallets():
    wallets = db.list_wallets()
    for w in wallets:
        encrypted_key = w.pop("encrypted_key", None)
        w["running"] = False
        w["balance"] = None
        if manager:
            eng = manager.engines.get(w["address"])
            if eng and eng.running:
                w["running"] = True
                try:
                    w["balance"] = eng.api.get_balance()
                except Exception:
                    pass
            elif encrypted_key and encryption_key:
                try:
                    api = _get_cached_api(
                        w["address"], encrypted_key, w.get("funder", "")
                    )
                    w["balance"] = api.get_balance()
                except Exception:
                    pass
    return jsonify(wallets)


@app.route("/api/wallets", methods=["POST"])
@login_required
def api_add_wallet():
    data = request.get_json()
    raw_key = data.get("private_key", "")
    # Only keep hex characters (0-9, a-f, A-F) and 'x' for 0x prefix
    import re

    private_key = re.sub(r"[^0-9a-fA-Fx]", "", raw_key)
    # Remove any 0x prefix, then re-add it
    private_key = (
        private_key.lstrip("0x")
        if private_key.startswith("0x") or private_key.startswith("0X")
        else private_key
    )
    private_key = re.sub(r"[^0-9a-fA-F]", "", private_key)  # strip any remaining x
    if not private_key or len(private_key) != 64:
        return (
            jsonify(
                {
                    "error": f"私钥格式错误：需要64位十六进制字符，当前{len(private_key)}位"
                }
            ),
            400,
        )
    private_key = "0x" + private_key

    # Deposit wallet address (funder)
    funder = data.get("funder", "").strip()
    funder = re.sub(r"[^0-9a-fA-Fx]", "", funder)
    if funder and not funder.startswith("0x"):
        funder = "0x" + funder
    if not funder or len(funder) != 42:
        return jsonify({"error": "请输入有效的存款钱包地址（42位，0x开头）"}), 400

    from api.polymarket_api import PolymarketAPI

    try:
        api = PolymarketAPI(private_key, funder=funder)
        address = api.get_address()
    except Exception as e:
        return jsonify({"error": f"私钥无效: {e}"}), 400

    encrypted = encrypt(private_key, encryption_key)
    try:
        db.add_wallet(address, encrypted, funder)
    except Exception:
        return jsonify({"error": "该钱包已存在"}), 400

    return jsonify({"ok": True, "address": address, "funder": funder})


@app.route("/api/wallets/<address>", methods=["DELETE"])
@login_required
def api_remove_wallet(address):
    if manager:
        manager.stop_wallet(address)
    db.remove_wallet(address)
    return jsonify({"ok": True})


@app.route("/api/wallets/<address>/toggle", methods=["POST"])
@login_required
def api_toggle_wallet(address):
    data = request.get_json()
    enabled = data.get("enabled", True)
    db.toggle_wallet(address, enabled)
    return jsonify({"ok": True})


# --- API: Engine Control ---


# --- Auto mode ---


@app.route("/api/engine/start-all", methods=["POST"])
@login_required
def api_start_all():
    if manager:
        manager.start_all()
    return jsonify({"ok": True})


@app.route("/api/engine/stop-all", methods=["POST"])
@login_required
def api_stop_all():
    if manager:
        manager.stop_all()
    return jsonify({"ok": True, "message": "止损监控已停止，请注意现有持仓风险"})


@app.route("/api/engine/restart", methods=["POST"])
@login_required
def api_restart():
    if manager:
        manager.restart_all()
    return jsonify({"ok": True})


# --- Manual mode ---


@app.route("/api/engine/start-monitors", methods=["POST"])
@login_required
def api_start_monitors():
    if manager:
        manager.start_monitors()
    return jsonify({"ok": True})


@app.route("/api/engine/scan", methods=["POST"])
@login_required
def api_scan_markets():
    if manager:
        import threading

        threading.Thread(target=manager.scan_markets, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/engine/place-orders", methods=["POST"])
@login_required
def api_place_orders():
    if manager:
        manager.place_all_orders()
    return jsonify({"ok": True})


@app.route("/api/engine/cancel-all", methods=["POST"])
@login_required
def api_cancel_all_buy():
    if manager:
        manager.cancel_all_buy_orders()
    return jsonify({"ok": True})


@app.route("/api/engine/<address>/start", methods=["POST"])
@login_required
def api_start_wallet(address):
    if manager:
        manager.start_wallet(address)
    return jsonify({"ok": True})


@app.route("/api/engine/<address>/stop", methods=["POST"])
@login_required
def api_stop_wallet(address):
    if manager:
        manager.stop_wallet(address)
    return jsonify({"ok": True, "message": "止损监控已停止，请注意现有持仓风险"})


# --- API: Orders ---


@app.route("/api/orders", methods=["GET"])
@login_required
def api_get_orders():
    wallet = request.args.get("wallet")
    orders = db.get_open_orders(wallet)
    return jsonify(orders)


@app.route("/api/orders/<order_id>/cancel", methods=["POST"])
@login_required
def api_cancel_order(order_id):
    orders = db.get_open_orders()
    order = next((o for o in orders if o["order_id"] == order_id), None)
    if not order:
        return jsonify({"error": "订单不存在"}), 404

    if manager:
        eng = manager.engines.get(order["wallet"])
        if eng and eng.running:
            eng.api.cancel_order(order_id)

    db.update_order_status(order_id, "cancelled")
    return jsonify({"ok": True})


@app.route("/api/orders/cancel-batch", methods=["POST"])
@login_required
def api_cancel_batch():
    data = request.get_json()
    order_ids = data.get("order_ids", [])
    for oid in order_ids:
        orders = db.get_open_orders()
        order = next((o for o in orders if o["order_id"] == oid), None)
        if order and manager:
            eng = manager.engines.get(order["wallet"])
            if eng and eng.running:
                eng.api.cancel_order(oid)
            db.update_order_status(oid, "cancelled")
    return jsonify({"ok": True})


# --- API: Positions ---


@app.route("/api/positions", methods=["GET"])
@login_required
def api_get_positions():
    wallet = request.args.get("wallet")
    positions = db.get_positions(wallet)
    if manager:
        for pos in positions:
            eng = manager.engines.get(pos["wallet"])
            if eng and eng.running:
                try:
                    pos["current_price"] = eng.api.get_last_trade_price(pos["token_id"])
                    pos["pnl"] = (pos["current_price"] - pos["buy_price"]) * pos["size"]
                    pos["stop_price"] = pos["buy_price"] * (
                        1 - db.get_settings()["stop_loss_pct"] / 100
                    )
                except Exception:
                    pos["current_price"] = None
    return jsonify(positions)


# --- API: History ---


@app.route("/api/history", methods=["GET"])
@login_required
def api_get_history():
    wallet = request.args.get("wallet")
    start = request.args.get("start", type=float)
    end = request.args.get("end", type=float)
    trades = db.get_trade_history(wallet, start, end)
    return jsonify(trades)


# --- API: Eligible Markets ---


@app.route("/api/eligible", methods=["GET"])
@login_required
def api_eligible_markets():
    """Get the latest list of eligible markets + scan status.

    During scanning: returns real-time data from memory.
    When idle: returns persisted data from database.
    """
    if not manager:
        # No manager yet, try loading from database
        markets = db.get_eligible_markets()
        return jsonify({"markets": markets, "last_scan_time": 0, "scan_status": "idle"})

    # During scanning, use memory (real-time updates)
    if manager.scan_status == "scanning":
        markets = manager.eligible_markets
    else:
        # Idle or done: prefer memory if available, else database
        markets = (
            manager.eligible_markets
            if manager.eligible_markets
            else db.get_eligible_markets()
        )

    return jsonify(
        {
            "markets": markets,
            "last_scan_time": manager.last_scan_time,
            "scan_status": manager.scan_status,
            "scan_progress": manager.scan_progress,
            "scan_checked": manager.scan_checked,
            "scan_total": manager.scan_total,
        }
    )


# --- API: Dashboard Summary ---


@app.route("/api/dashboard", methods=["GET"])
@login_required
def api_dashboard():
    wallets = db.list_wallets()
    total_orders = len(db.get_open_orders())
    total_positions = len(db.get_positions())
    trades = db.get_trade_history()
    total_pnl = sum(t.get("pnl", 0) for t in trades)

    wallet_summaries = []
    for w in wallets:
        w_orders = db.get_open_orders(w["address"])
        w_positions = db.get_positions(w["address"])
        balance = None
        running = False
        if manager:
            eng = manager.engines.get(w["address"])
            if eng and eng.running:
                running = True
                try:
                    balance = eng.api.get_balance()
                except Exception:
                    pass
            elif not running and encryption_key:
                try:
                    api = _get_cached_api(
                        w["address"], w["encrypted_key"], w.get("funder", "")
                    )
                    balance = api.get_balance()
                except Exception:
                    pass
        wallet_summaries.append(
            {
                "address": w["address"],
                "enabled": w["enabled"],
                "running": running,
                "balance": balance,
                "open_orders": len(w_orders),
                "positions": len(w_positions),
            }
        )

    return jsonify(
        {
            "total_orders": total_orders,
            "total_positions": total_positions,
            "total_pnl": total_pnl,
            "wallets": wallet_summaries,
        }
    )
