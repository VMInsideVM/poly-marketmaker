"""web/routes.py — Flask routes and API endpoints."""

import os
import re
import sys
import hashlib
import logging
import sqlite3
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
from utils.crypto import derive_key, encrypt, decrypt
from engine.monitor_status import get_snapshot
from engine.market_links import enrich_with_market_meta, ensure_market_meta
from engine.blacklist_ops import buy_order_ids_for_condition
from engine.take_profit import effective_theta_stop
from config import DB_PATH, HOST, PORT
from web import update as updater
from version import __version__

logger = logging.getLogger(__name__)

# When packaged by PyInstaller the source tree isn't on disk; templates and
# static files are bundled under sys._MEIPASS/web/. In dev, _MEIPASS is absent
# so we fall back to the project root (parent of this web/ package).
_BASE = getattr(
    sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
app = Flask(
    __name__,
    template_folder=os.path.join(_BASE, "web", "templates"),
    static_folder=os.path.join(_BASE, "web", "static"),
)
app.secret_key = os.urandom(32)


@app.context_processor
def _inject_version():
    """让所有模板都能用 {{ app_version }} 显示当前版本(不触网)。"""
    return {"app_version": __version__}


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


def _get_cached_api(
    address: str,
    encrypted_key: str,
    funder: str = "",
    signature_type: int = 2,
    proxy: str = "",
):
    """Get or create a cached PolymarketAPI instance for balance queries."""
    if address in _api_cache:
        return _api_cache[address]
    from api.polymarket_api import PolymarketAPI

    pk = decrypt(encrypted_key, encryption_key)
    api = PolymarketAPI(
        pk, signature_type=signature_type, funder=funder or None, proxy=proxy or None
    )
    _api_cache[address] = api
    return api


def _wallet_apis(only: str = None) -> dict:
    """Return {address: PolymarketAPI} for enabled wallets (optionally one)."""
    out = {}
    wallets = db.list_wallets()
    for w in wallets:
        if not w.get("enabled"):
            continue
        addr = w["address"]
        if only and addr != only:
            continue
        if manager and manager.engines.get(addr) and manager.engines[addr].running:
            out[addr] = manager.engines[addr].api
        else:
            try:
                from api.polymarket_api import PolymarketAPI

                pk = decrypt(w["encrypted_key"], encryption_key)
                out[addr] = PolymarketAPI(
                    pk,
                    signature_type=w.get("signature_type", 2),
                    funder=w.get("funder") or None,
                    proxy=w.get("proxy") or None,
                )
            except Exception as e:
                app.logger.error("API build failed for %s: %s", addr, e)
    return out


def _gamma_fetch(condition_ids):
    """Gamma 解析回调(惰性导入 PolymarketAPI,沿用本文件的惰性导入约定)。"""
    from api.polymarket_api import PolymarketAPI

    return PolymarketAPI.gamma_markets_by_condition(condition_ids)


def _enrich_rows(rows, id_key):
    """给 rows 补市场名+链接:先用 Gamma 兜底补全 market_meta,再 enrich。"""
    meta = ensure_market_meta([r.get(id_key, "") for r in rows], db, _gamma_fetch)
    enrich_with_market_meta(rows, meta, id_key)


def _derive_display_metrics(markets):
    """给 eligible 行派生展示指标(不落库):奖励范围、盘口价差、方向。

    奖励范围/盘口价差由候选池内存中的 _orderbooks 现算(扫描/下单轮刷过书才有);无书
    (闲时或 DB 行)置 None,前端显「—」,避免落库占位值 0/1/-1 被误读。盘口价差对二元市场
    两侧相等;奖励范围/方向取首个有订单簿的 token 作代表(完整两侧见展开预演)。最后剥掉
    _orderbooks 以免撑大响应。"""
    from engine.strategy import reward_price_range

    for m in markets:
        rr_min = rr_max = sp = None
        books = m.get("_orderbooks") or {}
        for tok in m.get("tokens", []) or []:
            b = books.get(tok.get("token_id", ""))
            if not b:
                continue
            bids = sorted(
                b.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            asks = sorted(b.get("asks", []), key=lambda x: float(x["price"]))
            if not bids or not asks:
                continue
            bb, ba = float(bids[0]["price"]), float(asks[0]["price"])
            rr_min, rr_max = reward_price_range(
                (bb + ba) / 2, float(m.get("rewards_max_spread", 2))
            )
            sp = (ba - bb) * 100
            if not m.get("outcome"):
                m["outcome"] = tok.get("outcome", "")
            break
        m["reward_range_min"] = rr_min
        m["reward_range_max"] = rr_max
        m["spread_cents"] = sp
        m.pop("_orderbooks", None)
    return markets


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
    # 退出时清掉进程内的解密密钥与缓存的 API 客户端,缩短密钥在内存中的存活窗口(F9)。
    # 运行中的引擎持有各自的 api 实例、不依赖此模块级密钥;重新登录会重新派生同一密钥。
    set_encryption_key(None)
    _api_cache.clear()
    return redirect(url_for("login"))


# --- Pages ---


@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/markets")
@login_required
def markets_page():
    return render_template("markets.html")


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


@app.route("/logs")
@login_required
def logs_page():
    return render_template("logs.html")


@app.route("/blacklist")
@login_required
def blacklist_page():
    return render_template("blacklist.html")


@app.route("/help")
@login_required
def help_page():
    return render_template("help.html")


@app.route("/networth")
@login_required
def networth_page():
    return render_template("networth.html")


@app.route("/api/monitor-status", methods=["GET"])
@login_required
def api_monitor_status():
    snap = get_snapshot()
    _enrich_rows(snap.get("rows", []), "market")
    return jsonify(snap)


# --- API: Settings ---


@app.route("/api/settings", methods=["GET"])
@login_required
def api_get_settings():
    # 引擎级(settings)+ 默认模板的策略级参数合并返回,供现有「全局参数」表单
    # 编辑。多模板 UI 留给 SP6;此处把默认模板当作全局策略参数面板。
    merged = dict(db.get_settings())
    merged.update(db.get_template(db.get_default_template_id()))
    return jsonify(merged)


@app.route("/api/settings", methods=["POST"])
@login_required
def api_save_settings():
    from config import ENGINE_DEFAULTS, TEMPLATE_DEFAULTS

    data = request.get_json() or {}
    engine = {k: v for k, v in data.items() if k in ENGINE_DEFAULTS}
    strategy = {k: v for k, v in data.items() if k in TEMPLATE_DEFAULTS}
    if "size_tiers" in strategy:
        from engine.tiers import validate_size_tiers

        tiers, err = validate_size_tiers(strategy["size_tiers"])
        if err:
            return jsonify({"error": err}), 400
        strategy["size_tiers"] = tiers
    if engine:
        db.save_settings(engine)
    if strategy:
        db.save_template(db.get_default_template_id(), strategy)
    return jsonify(
        {
            "ok": True,
            "message": "参数已保存。如需立即生效，请重启引擎；否则将在下次启动时生效。",
        }
    )


@app.route("/api/categories", methods=["GET"])
@login_required
def api_categories():
    if manager is None:
        return jsonify({"ready": False, "categories": [], "other_count": 0})
    try:
        refresh = request.args.get("refresh") in ("1", "true", "yes")
        return jsonify(manager.category_catalog(refresh=refresh))
    except Exception as e:
        return jsonify(
            {"ready": False, "categories": [], "other_count": 0, "error": str(e)}
        )


# --- API: Templates (多模板 CRUD + 钱包绑定) ---


@app.route("/api/templates", methods=["GET"])
@login_required
def api_list_templates():
    default_id = db.get_default_template_id()
    return jsonify(
        [
            {"id": t["id"], "name": t["name"], "is_default": t["id"] == default_id}
            for t in db.list_templates()
        ]
    )


@app.route("/api/templates", methods=["POST"])
@login_required
def api_create_template():
    name = ((request.get_json() or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "模板名不能为空"}), 400
    try:
        tid = db.create_template(name)
    except sqlite3.IntegrityError:
        return jsonify({"error": "模板名已存在"}), 400
    return jsonify({"id": tid, "name": name})


@app.route("/api/templates/<int:tid>", methods=["GET"])
@login_required
def api_get_template(tid):
    return jsonify(db.get_template(tid))


@app.route("/api/templates/<int:tid>", methods=["PUT"])
@login_required
def api_save_template(tid):
    from config import TEMPLATE_DEFAULTS

    data = request.get_json() or {}
    strategy = {k: v for k, v in data.items() if k in TEMPLATE_DEFAULTS}
    if "size_tiers" in strategy:
        from engine.tiers import validate_size_tiers

        tiers, err = validate_size_tiers(strategy["size_tiers"])
        if err:
            return jsonify({"error": err}), 400
        strategy["size_tiers"] = tiers
    db.save_template(tid, strategy)
    return jsonify({"ok": True})


@app.route("/api/templates/<int:tid>/name", methods=["PUT"])
@login_required
def api_rename_template(tid):
    name = ((request.get_json() or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "模板名不能为空"}), 400
    try:
        db.rename_template(tid, name)
    except sqlite3.IntegrityError:
        return jsonify({"error": "模板名已存在"}), 400
    return jsonify({"ok": True})


@app.route("/api/templates/<int:tid>", methods=["DELETE"])
@login_required
def api_delete_template(tid):
    try:
        db.delete_template(tid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/wallets/<address>/template", methods=["POST"])
@login_required
def api_set_wallet_template(address):
    tid = (request.get_json() or {}).get("template_id")
    db.set_wallet_template(address, int(tid))
    return jsonify({"ok": True})


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
                        w["address"],
                        encrypted_key,
                        w.get("funder", ""),
                        w.get("signature_type", 2),
                        w.get("proxy", ""),
                    )
                    w["balance"] = api.get_balance()
                except Exception:
                    pass
    return jsonify(wallets)


def _clean_private_key(raw: str):
    """Sanitize a hex private key. Returns (key_with_0x, None) or (None, error)."""
    pk = re.sub(r"[^0-9a-fA-Fx]", "", raw or "")
    if pk.startswith("0x") or pk.startswith("0X"):
        pk = pk[2:]
    pk = re.sub(r"[^0-9a-fA-F]", "", pk)  # strip any remaining x
    if not pk or len(pk) != 64:
        return None, f"私钥格式错误：需要64位十六进制字符，当前{len(pk)}位"
    return "0x" + pk, None


def _clean_funder(raw: str):
    """Sanitize an optional deposit-wallet address. Returns (funder, None) or (None, error)."""
    f = re.sub(r"[^0-9a-fA-Fx]", "", (raw or "").strip())
    if f and not f.startswith("0x"):
        f = "0x" + f
    if f and len(f) != 42:
        return None, "存款钱包地址格式错误：需要42位、0x开头"
    return f, None


@app.route("/api/wallets/preview", methods=["POST"])
@login_required
def api_preview_wallet():
    """Compute the EOA + auto-derived deposit (Safe) address WITHOUT storing.

    Lets the import UI show the auto-derived address for the user to compare
    against polymarket.com before committing. Pure derivation, no network.
    """
    data = request.get_json()
    private_key, err = _clean_private_key(data.get("private_key", ""))
    if err:
        return jsonify({"error": err}), 400
    from api.polymarket_api import derive_deposit_address, eoa_from_key

    try:
        eoa = eoa_from_key(private_key)
        derived_funder = derive_deposit_address(eoa)
    except Exception as e:
        return jsonify({"error": f"私钥无效: {e}"}), 400
    return jsonify({"address": eoa, "derived_funder": derived_funder})


@app.route("/api/wallets", methods=["POST"])
@login_required
def api_add_wallet():
    data = request.get_json()
    private_key, err = _clean_private_key(data.get("private_key", ""))
    if err:
        return jsonify({"error": err}), 400

    # Optional deposit-wallet (funder) override. Normally left blank and
    # auto-derived from the private key; the user may supply one when the
    # auto-derived address doesn't match polymarket.com/settings.
    funder, err = _clean_funder(data.get("funder", ""))
    if err:
        return jsonify({"error": err}), 400

    # 该钱包专属代理(明文存原串);此后含本次导入探测在内的所有网络活动都走它。
    proxy = (data.get("proxy") or "").strip()
    # 可选备注(纯展示,截断 40 字)。
    remark = (data.get("remark") or "").strip()[:40]

    from api.polymarket_api import (
        PolymarketAPI,
        derive_deposit_address,
        eoa_from_key,
        pick_funded_sig_type,
        resolve_signature_type,
    )

    # Detect the account type by asking the CLOB which signature type's derived
    # wallet actually holds collateral (EOA=0 / proxy=1 / safe=2 / EIP-1271
    # smart wallet=3). The balance query ignores the funder and derives the
    # address server-side, so the funded type IS the real account type. Fall
    # back to the funder-vs-derived-Safe heuristic only when nothing is funded
    # yet (empty account — re-import after depositing to re-detect).
    try:
        derived_safe = derive_deposit_address(eoa_from_key(private_key))
        provisional = resolve_signature_type(derived_safe, funder)
        api = PolymarketAPI(
            private_key,
            signature_type=provisional,
            funder=funder or None,
            proxy=proxy or None,
        )
        address = api.get_address()
        funder = api.get_funder()
        detected = pick_funded_sig_type(api.balance_by_sig_types())
        sig_type = detected if detected is not None else provisional
    except Exception as e:
        return jsonify({"error": f"私钥无效: {e}"}), 400

    encrypted = encrypt(private_key, encryption_key)
    try:
        db.add_wallet(address, encrypted, funder, sig_type, proxy=proxy, remark=remark)
    except Exception:
        return jsonify({"error": "该钱包已存在"}), 400

    _api_cache.pop(address, None)  # 重新导入可能改了 sig/funder,清掉旧缓存
    return jsonify(
        {"ok": True, "address": address, "funder": funder, "signature_type": sig_type}
    )


@app.route("/api/wallets/<address>", methods=["DELETE"])
@login_required
def api_remove_wallet(address):
    if manager:
        manager.stop_wallet(address)
    db.remove_wallet(address)
    _api_cache.pop(
        address, None
    )  # 丢弃旧的余额查询客户端,防止重导入后命中旧 sig/funder
    return jsonify({"ok": True})


@app.route("/api/wallets/<address>/proxy", methods=["PUT"])
@login_required
def api_set_wallet_proxy(address):
    """设置/清空某钱包的 IP 代理(明文存)。代理变更在下次启动该钱包引擎时生效;
    清掉缓存 api,使路由侧的余额查询按新代理重建。"""
    proxy = ((request.get_json() or {}).get("proxy") or "").strip()
    db.set_wallet_proxy(address, proxy)
    _api_cache.pop(address, None)
    return jsonify({"ok": True})


@app.route("/api/wallets/<address>/remark", methods=["PUT"])
@login_required
def api_set_wallet_remark(address):
    """设置/清空某钱包备注(纯展示,不影响任何 API 客户端;空串=清空,截断到 40 字)。"""
    remark = ((request.get_json() or {}).get("remark") or "").strip()[:40]
    db.set_wallet_remark(address, remark)
    return jsonify({"ok": True})


@app.route("/api/wallets/<address>/toggle", methods=["POST"])
@login_required
def api_toggle_wallet(address):
    data = request.get_json()
    enabled = data.get("enabled", True)
    db.toggle_wallet(address, enabled)
    return jsonify({"ok": True})


@app.route("/api/debug/balance-sigs")
@login_required
def api_debug_balance_sigs():
    """诊断:每个钱包在 sig 0/1/2/3 下的 COLLATERAL 余额,定位正确签名类型。"""
    out = {}
    for addr, api in _wallet_apis().items():
        try:
            out[addr] = {
                "eoa": api.get_address(),
                "funder": api.get_funder(),
                "balance_by_sig": api.balance_by_sig_types(),
            }
        except Exception as e:
            out[addr] = {"error": str(e)}
    return jsonify(out)


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
    if not manager:
        return jsonify({"ok": False, "started": False, "scanning": False})
    force = request.args.get("force") in ("1", "true", "yes")
    # force=False 且已在扫描 -> started=False + scanning=True,前端据此弹确认;
    # force=True(用户确认)-> 接管当前扫描、用最新配置重来。
    started = manager.start_scan_async(force=force)
    return jsonify({"ok": True, "started": started, "scanning": not started})


@app.route("/api/engine/scan-status", methods=["GET"])
@login_required
def api_scan_status():
    """轻量扫描状态(不含市场列表),供全局侧边栏进度指示在任意页面轮询。"""
    if not manager:
        return jsonify(
            {"scan_status": "idle", "scan_checked": 0, "scan_total": 0, "found": 0}
        )
    return jsonify(
        {
            "scan_status": manager.scan_status,
            "scan_checked": manager.scan_checked,
            "scan_total": manager.scan_total,
            "found": len(manager.eligible_markets or []),
            "last_scan_time": manager.last_scan_time,
        }
    )


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


@app.route("/api/engine/test-place-orders", methods=["POST"])
@login_required
def api_test_place_orders():
    if not manager:
        return jsonify({"ok": False, "message": "引擎未启动"})
    return jsonify(manager.test_place_orders())


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
    result, errors = [], []
    for addr, api in _wallet_apis(wallet).items():
        try:
            orders = api.get_open_orders()
        except Exception as e:
            errors.append({"wallet": addr, "msg": str(e)})
            continue
        buy_ids = [o["id"] for o in orders if o.get("side") == "BUY"]
        scoring = {}
        try:
            scoring = api.are_orders_scoring(buy_ids)
        except Exception as e:
            app.logger.warning("scoring failed for %s: %s", addr, e)
        for o in orders:
            result.append(
                {
                    "wallet": addr,
                    "order_id": o.get("id"),
                    "market": o.get("market"),
                    "asset_id": o.get("asset_id"),
                    "side": o.get("side"),
                    "outcome": o.get("outcome"),
                    "price": float(o.get("price", 0) or 0),
                    "original_size": float(o.get("original_size", 0) or 0),
                    "size_matched": float(o.get("size_matched", 0) or 0),
                    "created_at": o.get("created_at"),
                    "scoring": (
                        scoring.get(o.get("id")) if o.get("side") == "BUY" else None
                    ),
                }
            )
    _enrich_rows(result, "market")
    return jsonify({"orders": result, "errors": errors})


@app.route("/api/orders/cancel-batch", methods=["POST"])
@login_required
def api_cancel_batch():
    data = request.get_json() or {}
    items = data.get("orders", [])  # [{order_id, wallet}, ...]
    by_wallet = {}
    for it in items:
        w, oid = it.get("wallet"), it.get("order_id")
        if not w or not oid:
            continue
        by_wallet.setdefault(w, []).append(oid)
    apis = _wallet_apis()
    for addr, ids in by_wallet.items():
        api = apis.get(addr)
        if api and ids:
            try:
                api.cancel_orders(ids)
            except Exception as e:
                app.logger.error("cancel-batch failed for %s: %s", addr, e)
    return jsonify({"ok": True})


@app.route("/api/orders/<order_id>/cancel", methods=["POST"])
@login_required
def api_cancel_order(order_id):
    wallet = request.args.get("wallet")
    apis = _wallet_apis(wallet)
    if wallet:
        api = apis.get(wallet)
        if api is None:
            return jsonify({"error": "钱包不可用"}), 404
        try:
            api.cancel_orders([order_id])
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})
    # no wallet specified: try every enabled wallet (single _wallet_apis call)
    for a in apis.values():
        try:
            a.cancel_orders([order_id])
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/orders/cancel-all-buys", methods=["POST"])
@login_required
def api_cancel_all_buys():
    data = request.get_json(silent=True) or {}
    wallet = data.get("wallet")
    for addr, api in _wallet_apis(wallet).items():
        try:
            orders = api.get_open_orders()
            buy_ids = [o["id"] for o in orders if o.get("side") == "BUY"]
            if buy_ids:
                api.cancel_orders(buy_ids)
        except Exception as e:
            app.logger.error("cancel-all-buys failed for %s: %s", addr, e)
    return jsonify({"ok": True})


# --- API: Positions ---


@app.route("/api/positions", methods=["GET"])
@login_required
def api_get_positions():
    wallet = request.args.get("wallet")
    out = []
    for addr, api in _wallet_apis(wallet).items():
        # 止损是策略级参数,按各钱包自己的模板取(每钱包可不同)。可配置:按比例(占成本%)
        # 或按固定金额(美分)。展示价用 avg 现算(仅供参考,实际离场以 get_trades 成本为准)。
        tmpl = db.get_template_for(addr)
        stop_mode = tmpl.get("stop_loss_mode", "percent")
        stop_percent = tmpl.get("stop_loss_percent", 20)
        stop_cents = tmpl.get("theta_stop_cents", 5)
        try:
            for p in api.get_user_positions(api.get_funder()):
                avg = float(p.get("avgPrice", 0) or 0)
                cur = float(p.get("curPrice", 0) or 0)
                size = float(p.get("size", 0) or 0)
                eff = effective_theta_stop(avg, stop_mode, stop_percent, stop_cents)
                out.append(
                    {
                        "wallet": addr,
                        "market_name": p.get("title", p.get("conditionId", "")),
                        "condition_id": p.get("conditionId", ""),
                        "outcome": p.get("outcome", ""),
                        "buy_price": avg,
                        "size": size,
                        "current_price": cur,
                        "stop_price": (
                            max(0.0, avg - eff) if eff is not None else None
                        ),
                        "pnl": (cur - avg) * size,
                    }
                )
        except Exception as e:
            app.logger.warning("positions failed for %s: %s", addr, e)
    _enrich_rows(out, "condition_id")
    return jsonify(out)


# --- API: History ---


@app.route("/api/history", methods=["GET"])
@login_required
def api_get_history():
    wallet = request.args.get("wallet")
    start = request.args.get("start", type=float)
    end = request.args.get("end", type=float)
    trades = db.get_trade_history(wallet, start, end)
    return jsonify(trades)


@app.route("/api/actions", methods=["GET"])
@login_required
def api_get_actions():
    wallet = request.args.get("wallet")
    start = request.args.get("start", type=float)
    end = request.args.get("end", type=float)
    types = request.args.get("types")
    action_types = types.split(",") if types else None
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    page_size = request.args.get("page_size", default=100, type=int) or 100
    page_size = min(500, max(1, page_size))
    total = db.count_actions(wallet, start, end, action_types)
    rows = db.get_actions(
        wallet,
        start,
        end,
        action_types,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    _enrich_rows(rows, "market_id")
    return jsonify({"rows": rows, "total": total, "page": page, "page_size": page_size})


# --- API: Blacklist ---


@app.route("/api/blacklist", methods=["GET"])
@login_required
def api_get_blacklist():
    rows = db.get_blacklist()
    _enrich_rows(rows, "condition_id")
    return jsonify(rows)


@app.route("/api/blacklist", methods=["POST"])
@login_required
def api_add_blacklist():
    data = request.get_json(silent=True) or {}
    cid = (data.get("condition_id") or "").strip()
    note = (data.get("note") or "").strip()
    if not cid:
        return jsonify({"error": "condition_id 不能为空"}), 400
    db.add_to_blacklist(cid, note)
    # 撤掉所有钱包挂在该 condition_id 的买单(止盈卖单/持仓不动)
    cancelled = 0
    for addr, api in _wallet_apis().items():
        try:
            ids = buy_order_ids_for_condition(api.get_open_orders(), cid)
            if ids:
                api.cancel_orders(ids)
                cancelled += len(ids)
        except Exception as e:
            app.logger.error("blacklist cancel for %s failed: %s", addr, e)
    return jsonify({"ok": True, "cancelled": cancelled})


@app.route("/api/blacklist/<condition_id>", methods=["DELETE"])
@login_required
def api_remove_blacklist(condition_id):
    db.remove_from_blacklist(condition_id)
    return jsonify({"ok": True})


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
        markets = [dict(m) for m in db.get_eligible_markets()]
        _enrich_rows(markets, "market_id")
        _derive_display_metrics(markets)
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

    # Enrich shallow copies so we never mutate the live in-memory eligible list
    # (mutated by the scanner thread) or the DB rows.
    markets = [dict(m) for m in markets]
    _enrich_rows(markets, "market_id")
    _derive_display_metrics(markets)

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


@app.route("/api/markets/<market_id>/ladder", methods=["GET"])
@login_required
def api_market_ladder(market_id):
    # 预演里任何一步(建 API / 查余额 / 抓订单簿 / 推演)抛异常都要返回 JSON 错误,别让
    # Flask 吐 HTML 报错页 —— 否则前端 r.json() 直接炸「Unexpected token '<'」,真正的原因
    # 反被 HTML 页盖住。同时打完整 traceback 到日志便于定位。
    try:
        return _ladder_payload(market_id)
    except Exception as e:
        logger.exception("预演失败 market=%s", market_id)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


def _ladder_payload(market_id):
    from engine.laddering import preview_gap_single_market
    from engine.strategy import reward_price_range
    from engine.positions import held_side_info
    from engine.tiers import tier_for

    wallet = request.args.get("wallet")
    apis = _wallet_apis(wallet)
    if not apis:
        return jsonify({"error": "钱包不可用"}), 404
    addr, api = next(iter(apis.items()))
    tmpl = db.get_template_for(addr)
    size_tiers = tmpl.get("size_tiers") or []
    max_exposure_usd = float(tmpl.get("max_exposure_usd", 250))
    max_exposure_shares = int(tmpl.get("max_exposure_shares", 500))

    src = (
        manager.eligible_markets
        if (manager and manager.eligible_markets)
        else db.get_eligible_markets()
    )
    rows = [dict(m) for m in src if m.get("market_id") == market_id]
    if not rows:
        return jsonify({"error": "市场不在 eligible 列表"}), 404

    try:
        positions = api.get_user_positions(api.get_funder())
    except Exception:
        positions = []
    _, held_value, held_shares = held_side_info(positions)
    balance = api.get_balance()
    budget = max(0.0, min(balance, max_exposure_usd) - held_value.get(market_id, 0.0))
    shares_budget = max(0, max_exposure_shares - int(held_shares.get(market_id, 0.0)))

    # 每侧 token 来自市场的 tokens 列表(内存候选池形态);DB 落库形态只有单个 token_id
    # 列、无 tokens 列表 —— 退化为单侧。奖励区间/最低份数是市场级,取自 market。
    market = rows[0]
    min_size = int(market.get("rewards_min_size", 0) or 0)
    token_sides = market.get("tokens") or []
    if not token_sides and market.get("token_id"):
        token_sides = [
            {"token_id": market["token_id"], "outcome": market.get("outcome", "")}
        ]

    sides_in = []
    for tok in token_sides[:2]:
        tid = tok.get("token_id")
        if not tid:
            continue
        ob = api.get_orderbook(tid)
        bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
        if not bids or not asks:
            continue
        bb, ba = float(bids[0]["price"]), float(asks[0]["price"])
        mid = (bb + ba) / 2
        rmin, rmax = reward_price_range(mid, float(market.get("rewards_max_spread", 2)))
        sides_in.append(
            {
                "outcome": tok.get("outcome", ""),
                "token_id": tid,
                "min_size": min_size,
                "reward_range_min": rmin,
                "reward_range_max": rmax,
                "best_bid": bb,
                "best_ask": ba,
                "spread_cents": (ba - bb) * 100,
                "bids": bids,
            }
        )
    a = sides_in[0] if sides_in else None
    b = sides_in[1] if len(sides_in) > 1 else None
    # 网关式单档预演:逐市场按断层单档规则判(选中/跳过 + 原因)。
    tier = tier_for(size_tiers, min_size)
    if tier is None:
        # 无匹配档位模块:该市场不挂,预演每侧给出跳过原因(与下单层口径一致)。
        sides = [
            {
                "outcome": s.get("outcome", ""),
                "token_id": s.get("token_id", ""),
                "best_bid": s.get("best_bid"),
                "best_ask": s.get("best_ask"),
                "spread_cents": s.get("spread_cents"),
                "reward_range": [s["reward_range_min"], s["reward_range_max"]],
                "rule": None,
                "rule_label": "无匹配档位",
                "max_gap": 0.0,
                "min_coeff": None,
                "high_sum": None,
                "gate_passed": False,
                "action": "skip",
                "chosen_index": None,
                "chosen_price": None,
                "chosen_shares": None,
                "skip_reason": f"无匹配档位模块（最低份额 {min_size}）",
                "cliff": False,
                "levels": [],
            }
            for s in sides_in
        ]
    else:
        preview = preview_gap_single_market(
            a,
            b,
            tier.get("amount_value_table") or None,
            float(tmpl.get("gap_wide_cents", 10)),
            float(tmpl.get("gap_mid_cents", 5)),
            float(tier.get("gap_high_coeff_sum_min", 20)),
            float(tier.get("rule1_min_coeff", 0)),
            float(tier.get("rule2_min_coeff", 0)),
            float(tier.get("rule3_min_coeff", 0)),
            float(tmpl.get("cliff_probe_cents", 2)),
            shares=int(tier.get("shares", 0) or 0) or None,
        )
        sides = [preview[k] for k in ("a", "b") if preview.get(k)]
    return jsonify(
        {
            "market_id": market_id,
            "market_name": rows[0].get("market_name", ""),
            "budget_usd": budget,
            "shares_budget": shares_budget,
            "sides": sides,
            "placement_mode": "gap_single",
        }
    )


# --- API: 净值历史 ---


@app.route("/api/networth", methods=["GET"])
@login_required
def api_networth():
    wallet = (request.args.get("wallet") or "").strip()
    if not wallet:
        return jsonify({"error": "缺少 wallet 参数"}), 400
    try:
        days = int(request.args.get("days", 90))
    except (TypeError, ValueError):
        days = 90
    return jsonify({"wallet": wallet, "series": db.get_net_worth_daily(wallet, days)})


@app.route("/api/pnl", methods=["GET"])
@login_required
def api_pnl():
    """每日盈亏台账。wallet=<addr> 单钱包,wallet=all 全钱包按日期汇总。

    返回 {series:[{date,reward,rebate,sell_profit,loss,fee,net}], totals, cumulative_net}。
    日期按北京日回溯 days 天。
    """
    import time
    from engine.pnl import beijing_day

    wallet = (request.args.get("wallet") or "").strip()
    if not wallet:
        return jsonify({"error": "缺少 wallet 参数"}), 400
    try:
        days = int(request.args.get("days", 90))
    except (TypeError, ValueError):
        days = 90
    now = time.time()
    to_date = beijing_day(now)
    from_date = beijing_day(now - days * 86400)
    if wallet == "all":
        series = db.get_daily_pnl_all(from_date, to_date)
    else:
        series = db.get_daily_pnl(wallet, from_date, to_date)
    keys = ("reward", "rebate", "sell_profit", "loss", "fee", "net")
    totals = {k: round(sum(s[k] for s in series), 6) for k in keys}
    cum = 0.0
    cumulative_net = []
    for s in series:
        cum += s["net"]
        cumulative_net.append(round(cum, 6))
    return jsonify(
        {
            "wallet": wallet,
            "series": series,
            "totals": totals,
            "cumulative_net": cumulative_net,
        }
    )


# --- API: Dashboard Summary ---


@app.route("/api/dashboard", methods=["GET"])
@login_required
def api_dashboard():
    wallets = db.list_wallets()
    apis = _wallet_apis()
    total_orders = 0
    total_positions = 0
    trades = db.get_trade_history()
    total_pnl = sum(t.get("pnl", 0) for t in trades)

    wallet_summaries = []
    for w in wallets:
        balance = None
        running = False
        api = apis.get(w["address"])
        w_order_count = w_pos_count = None
        if api:
            try:
                oo = api.get_open_orders()
                w_order_count = len(oo)
                total_orders += w_order_count
            except Exception:
                pass
            try:
                w_pos_count = len(api.get_user_positions(api.get_funder()))
                total_positions += w_pos_count
            except Exception:
                pass
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
                    balance = _get_cached_api(
                        w["address"],
                        w["encrypted_key"],
                        w.get("funder", ""),
                        w.get("signature_type", 2),
                    ).get_balance()
                except Exception:
                    pass
        wallet_summaries.append(
            {
                "address": w["address"],
                "remark": w.get("remark", ""),
                "enabled": w["enabled"],
                "running": running,
                "balance": balance,
                "open_orders": w_order_count,
                "positions": w_pos_count,
            }
        )

    # 无启用档位模块的模板:绑定它的钱包一张单都不会挂,仪表盘要醒目提示。
    templates_without_tiers = []
    try:
        from engine.tiers import enabled_sizes

        for t in db.list_templates():
            if not enabled_sizes(db.get_template(t["id"]).get("size_tiers") or []):
                templates_without_tiers.append(t["name"])
    except Exception:
        templates_without_tiers = []

    return jsonify(
        {
            "total_orders": total_orders,
            "total_positions": total_positions,
            "total_pnl": total_pnl,
            "wallets": wallet_summaries,
            "templates_without_tiers": templates_without_tiers,
        }
    )


# --- API: 自动更新(免登录:启动时弹窗在登录前出现) ---


@app.route("/api/update/check", methods=["GET"])
def api_update_check():
    return jsonify(updater.check_update())


@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    result = updater.start_update(manager)
    return jsonify(result), (200 if result.get("ok") else 409)


@app.route("/api/update/status", methods=["GET"])
def api_update_status():
    return jsonify(updater.STATE.snapshot())
