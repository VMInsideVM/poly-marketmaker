"""核 ②:批量奖励端点的 rate_per_day 是否 == 每市场精确端点?

若全等,discover_candidates 里的每市场 get_rewards_for_market(N+1 请求)可整个删掉,
直接用批量 get_rewards_markets 已有的 rate_per_day —— 发现阶段零额外网络。

奖励端点是**静态**方法(无需钱包/登录)。默认直连(读的是公开奖励数据、不带单)。
要走代理就用 `with use_proxy("http://user:pass@host:port"):` 包住下面的调用。

跑法(在项目根):  python debug/check_reward_equivalence.py
"""

from api.polymarket_api import PolymarketAPI

SAMPLE = 80  # 抽样对比多少个市场(全量太多、够看出规律即可)


def _batch_rate(market):
    return sum(rc.get("rate_per_day", 0) for rc in market.get("rewards_config", []))


def _precise_rate(cid):
    raw = PolymarketAPI.get_rewards_for_market(cid)
    if not raw:
        return None
    return sum(
        rc.get("rate_per_day", 0) for rd in raw for rc in rd.get("rewards_config", [])
    )


def main():
    full = PolymarketAPI.get_rewards_markets()
    print(f"批量奖励市场数: {len(full)};抽样对比前 {SAMPLE} 个\n")
    mismatches, checked, none_precise = [], 0, 0
    for m in full[:SAMPLE]:
        cid = m.get("condition_id", "")
        if not cid:
            continue
        batch = _batch_rate(m)
        precise = _precise_rate(cid)
        checked += 1
        if precise is None:
            none_precise += 1
            continue
        if abs(batch - precise) > 1e-6:
            mismatches.append((cid, batch, precise))

    print(
        f"对比 {checked} 个市场,精确端点空 {none_precise} 个,不一致 {len(mismatches)} 个"
    )
    for cid, b, p in mismatches[:20]:
        print(f"  DIFF {cid}: 批量={b}  精确={p}  差={p - b:+.4f}")
    print()
    if not mismatches:
        print("✅ 全部一致 —— 可以安全删掉每市场 get_rewards_for_market,直接用批量值。")
    else:
        print(
            "❌ 有不一致 —— 每市场精确拉取不能删(会改变奖励门槛判定)。保留(已并行化,不慢)。"
        )


if __name__ == "__main__":
    main()
