"""scripts/spike_api_shapes.py — throwaway: print real API JSON shapes. Not a pytest test."""

import json, hashlib, requests
from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH
from api.polymarket_api import PolymarketAPI
from py_clob_client_v2.clob_types import TradeParams, OrdersScoringParams


def main():
    db = Database(DB_PATH)
    db.init()
    pw_hash, salt = db.get_password()
    key = derive_key(input("访问密码: "), salt)
    assert hashlib.sha256(key).hexdigest() == pw_hash, "密码错误"
    w = db.list_wallets()[0]
    api = PolymarketAPI(
        decrypt(w["encrypted_key"], key), funder=w.get("funder") or None
    )
    addr = api.get_address()
    funder = w.get("funder") or addr

    print("\n=== get_open_orders[0] ===")
    oo = api.client.get_open_orders()
    print(json.dumps(oo[:1], indent=2, default=str))

    print("\n=== get_trades (maker_address) [0..1] ===")
    tr = api.client.get_trades(TradeParams(maker_address=funder))
    print(json.dumps(tr[:2], indent=2, default=str))

    print("\n=== are_orders_scoring ===")
    ids = [o["id"] for o in oo if o.get("side") == "BUY"][:5]
    if ids:
        print(
            json.dumps(
                api.client.are_orders_scoring(OrdersScoringParams(orderIds=ids)),
                indent=2,
                default=str,
            )
        )
    else:
        print("no open buy orders to test scoring")

    print("\n=== Data API positions ===")
    for u in {addr, funder}:
        r = requests.get(
            "https://data-api.polymarket.com/positions", params={"user": u}, timeout=15
        )
        print(f"user={u} status={r.status_code}")
        print(json.dumps(r.json()[:2] if r.ok else r.text, indent=2, default=str))
    db.close()


if __name__ == "__main__":
    main()
