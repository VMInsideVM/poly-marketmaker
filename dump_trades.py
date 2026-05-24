# dump_trades.py — 手动验证 get_trades 真实结构（非 pytest）。
# 用法：python dump_trades.py   （会要求输入访问密码）
import json
import hashlib
from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH
from py_clob_client_v2.clob_types import TradeParams


def main():
    db = Database(DB_PATH)
    db.init()
    pw_hash, salt = db.get_password()
    if pw_hash is None:
        print("未设置密码")
        return
    password = input("请输入访问密码: ")
    key = derive_key(password, salt)
    if hashlib.sha256(key).hexdigest() != pw_hash:
        print("密码错误")
        return
    w = db.list_wallets()[0]
    private_key = decrypt(w["encrypted_key"], key)

    from api.polymarket_api import PolymarketAPI

    api = PolymarketAPI(private_key, funder=w.get("funder") or None)
    funder = api.get_funder()
    print(f"funder = {funder}")

    positions = api.get_user_positions(funder)
    positions = [p for p in positions if float(p.get("size", 0) or 0) > 0]
    if not positions:
        print("当前无持仓，无法验证；请在有持仓时再跑")
        return
    asset_id = positions[0]["asset"]
    print(f"用持仓 asset_id = {asset_id} 验证\n")

    with_maker = api.get_trades(TradeParams(maker_address=funder, asset_id=asset_id))
    no_maker = api.get_trades(TradeParams(asset_id=asset_id))
    print(f"带 maker_address: {len(with_maker)} 笔")
    print(f"不带 maker_address: {len(no_maker)} 笔\n")

    f = funder.lower()
    for tr in no_maker:
        maker_addrs = [
            str(mo.get("maker_address", "")).lower()
            for mo in tr.get("maker_orders", []) or []
        ]
        ours_in_makers = f in maker_addrs
        print(
            f"id={tr.get('id')} trader_side={tr.get('trader_side')} "
            f"top.side={tr.get('side')} top.asset={tr.get('asset_id')} "
            f"top.size={tr.get('size')} top.price={tr.get('price')} "
            f"我们在maker_orders={ours_in_makers}"
        )

    with open("trades_dump.json", "w", encoding="utf-8") as fh:
        json.dump(no_maker, fh, ensure_ascii=False, indent=2)
    print("\n完整返回已写入 trades_dump.json")


if __name__ == "__main__":
    main()
