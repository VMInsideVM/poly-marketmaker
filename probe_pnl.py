# probe_pnl.py — 每日盈亏台账 Phase 0 实盘探针（非 pytest，只读，不下单）。
#
# 用法：python probe_pnl.py        （会要求输入访问密码）
#
# 目的：打印真钱包的以下真实 JSON，据此锁定台账各字段形态（文档有几处"需实盘验证"）：
#   1) 做市奖励：CLOB /rewards/user/total（按天）、/rewards/user（按市场，看 earnings/asset_rate）
#   2) 活动流：Data API /activity —— REWARD / REDEEM / DEPOSIT / WITHDRAWAL / TRADE 各取样
#   3) 逐笔成交：get_trades —— fee_rate_bps 及 maker_orders 内的费率字段（算 taker 手续费用）
# 把完整结果写盘（probe_pnl_dump.json）供人工核对。跑完把终端输出贴回来即可。
import json
import hashlib
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH
from py_clob_client_v2.clob_types import TradeParams
from api.polymarket_api import PolymarketAPI, DATA_API_HOST
from api.proxy import use_proxy, http_get


def _utc_date(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _dump(obj, n=2):
    """紧凑打印前 n 条样本。"""
    return json.dumps(
        obj[:n] if isinstance(obj, list) else obj, ensure_ascii=False, indent=2
    )


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

    wallets = db.list_wallets()
    if not wallets:
        print("无钱包")
        return
    w = wallets[0]  # 用第一个钱包探针（够看字段形态；多钱包口径一致）
    private_key = decrypt(w["encrypted_key"], key)
    api = PolymarketAPI(
        private_key,
        signature_type=w.get("signature_type", 2),
        funder=w.get("funder") or None,
        proxy=w.get("proxy") or None,
    )
    funder = api.get_funder()
    print("=" * 78)
    print(f"钱包 funder = {funder} | signature_type = {w.get('signature_type')}")
    print("=" * 78)

    out = {"funder": funder}

    with use_proxy(api.proxy_url):
        # ---- 1) 做市奖励 ----
        print("\n【1) 做市奖励 /rewards/user/total（按天，UTC 日期）】")
        rewards_total = {}
        for d_ago in (1, 2, 3, 4):
            date = _utc_date(d_ago)
            try:
                r = api.client.get_total_earnings_for_user_for_day(date)
                rewards_total[date] = r
                print(f"  {date}: {json.dumps(r, ensure_ascii=False)}")
            except Exception as e:
                print(f"  {date}: 报错 {e}")
        out["rewards_total"] = rewards_total

        print("\n【1b) 做市奖励 /rewards/user（按市场，看 earnings/asset_rate 字段）】")
        rewards_by_market = {}
        for d_ago in (1, 2, 3):
            date = _utc_date(d_ago)
            try:
                r = api.client.get_earnings_for_user_for_day(date)
                rewards_by_market[date] = r
                print(f"  {date}: {len(r)} 条；样本:\n{_dump(r, 2)}")
                if r:
                    break  # 拿到有数据的一天即可
            except Exception as e:
                print(f"  {date}: 报错 {e}")
        out["rewards_by_market"] = rewards_by_market

        # ---- 2) 活动流 /activity ----
        print("\n【2) 活动流 /activity（按 type 分组取样）】")
        activity = []
        try:
            activity = http_get(
                f"{DATA_API_HOST}/activity",
                params={"user": funder, "limit": 500},
                timeout=20,
            ).json()
        except Exception as e:
            print(f"  /activity 报错: {e}")
        by_type = defaultdict(list)
        for a in activity if isinstance(activity, list) else []:
            by_type[a.get("type", "?")].append(a)
        print(
            f"  共 {len(activity)} 条；type 分布: "
            f"{ {t: len(v) for t, v in by_type.items()} }"
        )
        for t in (
            "REWARD",
            "REDEEM",
            "DEPOSIT",
            "WITHDRAWAL",
            "TRADE",
            "MERGE",
            "SPLIT",
            "CONVERSION",
        ):
            if by_type.get(t):
                print(
                    f"\n  -- type={t}（{len(by_type[t])} 条，样本）--\n{_dump(by_type[t], 2)}"
                )
        out["activity_by_type"] = {t: v for t, v in by_type.items()}

        # ---- 3) 逐笔成交 get_trades（fee 字段）----
        print("\n【3) get_trades 逐笔成交（fee_rate_bps / maker_orders 费率字段）】")
        trades = []
        try:
            trades = api.get_trades(TradeParams(maker_address=funder))
            print(f"  共 {len(trades)} 笔；前 3 笔:\n{_dump(trades, 3)}")
        except Exception as e:
            print(f"  get_trades 报错: {e}")
        out["trades_sample"] = trades[:5]

    with open("probe_pnl_dump.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)
    print("\n完整结果已写入 probe_pnl_dump.json（把终端输出贴回来即可）")


if __name__ == "__main__":
    main()
