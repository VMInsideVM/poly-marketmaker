"""check_api_assumptions.py — 发版前对真实 Polymarket 接口核对程序依赖的假设。

不是 pytest 测试(要联网),由 release.ps1 在构建前调用;也可以手动跑:

    python check_api_assumptions.py [--proxy http://127.0.0.1:7890]

为什么要有它:2026-10 连着三次「配置全对却不生效」,根因都是外部接口的某个假设悄悄
不成立、程序不报错照常跑——
- 奖励接口的 created_at / end_date 是**事件**级的,不是市场的;
- Gamma /markets?condition_ids= 默认只回 20 条;
- 「文化」品类的查询标签 culture 查回永远是 0 个(该用 pop-culture)。
这些都 fail-open,单元测试也测不出来(mock 照着我们以为的样子回)。这里尽量走程序自己
的代码路径去打真实接口,任何一条 FAIL 就退出码 1,release.ps1 据此中止发版。

新增对外部接口的依赖时,把它的假设也补一条到这里。
"""

import argparse
import sys
import time
from datetime import datetime, timezone

from api.polymarket_api import PolymarketAPI, POLYMARKET_HOST
from api.proxy import http_post, use_proxy
from config import CATEGORY_QUERY_TAG

results = []  # (级别, 名称, 说明)


def record(level, name, detail):
    results.append((level, name, detail))
    print(f"[{level}] {name}: {detail}", flush=True)


def _ts(s):
    """ISO / 'YYYY-MM-DD HH:MM:SS+00' 串 -> UTC 秒;解析不出返回 None。"""
    if not isinstance(s, str) or len(s) < 19:
        return None
    try:
        return (
            datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def check_category_tags():
    """每个 curated 品类的查询标签都要能查到奖励市场(查回 0 = 标签失效,该品类全落进「其他」)。"""
    dead = []
    for slug, tag in CATEGORY_QUERY_TAG.items():
        rows = PolymarketAPI.get_rewards_markets(tag_slug=tag, max_pages=1)
        if not rows:
            dead.append(f"{slug}({tag})")
    if dead:
        record("FAIL", "品类查询标签", "查回 0 个奖励市场: " + ", ".join(dead))
    else:
        record("PASS", "品类查询标签", f"{len(CATEGORY_QUERY_TAG)} 个标签都能查到市场")


def check_gamma_batch(sample):
    """按 condition_id 批量查 Gamma,一批 50 个必须一个不少(默认只回 20 条的坑)。"""
    cids = [m["condition_id"] for m in sample]
    got = PolymarketAPI.gamma_market_times(cids)
    missing = [c for c in cids if c not in got]
    if missing:
        record(
            "FAIL",
            "Gamma 批量查询",
            f"请求 {len(cids)} 个在交易的奖励市场,少回 {len(missing)} 个(例 {missing[0]})",
        )
    else:
        record("PASS", "Gamma 批量查询", f"{len(cids)} 个全部查到")
    return got


def report_event_level_drift(sample, gamma):
    """奖励接口的时间(事件级)和市场自己的差多少——只报告,不判失败。

    Gamma 查不到时程序退回用奖励接口的时间。创建时间:事件不会晚于其下市场,退回只会
    少拦;若这里出现「奖励接口创建时间晚于市场」,说明这个前提也变了,要回头看。结算日:
    两个方向都有(实测「停火持续到 11 月 30 日」所在事件 10-31 就结束),退回可能多拦也
    可能少拦。"""
    created_late = end_far = end_near = 0
    for m in sample:
        g = gamma.get(m["condition_id"]) or {}
        rc, gc = _ts(m.get("created_at")), _ts(g.get("created_at"))
        re_, ge = _ts(m.get("end_date")), _ts(g.get("end_date"))
        if rc and gc and rc > gc + 60:
            created_late += 1
        if re_ and ge and re_ > ge + 86400:
            end_far += 1
        if re_ and ge and re_ < ge - 86400:
            end_near += 1
    level = "WARN" if created_late else "INFO"
    record(
        level,
        "奖励接口与市场自己的时间",
        f"{len(sample)} 个里:创建时间晚于市场 {created_late} 个(应为 0)、"
        f"结算日晚一天以上 {end_far} 个、早一天以上 {end_near} 个",
    )


def check_resolution_field():
    """结算保护读 umaResolutionStatus(单数)。复数列表非空时单数必须也非空,否则就是改名了。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    from api.proxy import http_get

    resp = http_get(
        "https://gamma-api.polymarket.com/markets",
        params={"closed": "false", "limit": 200, "end_date_max": now},
        timeout=30,
    )
    resp.raise_for_status()
    markets = resp.json()
    pending = [
        m for m in markets if m.get("umaResolutionStatuses") not in (None, "", "[]")
    ]
    renamed = [m for m in pending if not m.get("umaResolutionStatus")]
    if renamed:
        record(
            "FAIL",
            "结算状态字段",
            f"{len(renamed)} 个市场有结算记录但 umaResolutionStatus 为空,字段可能改名了",
        )
    elif not pending:
        record("WARN", "结算状态字段", "眼下没找到正在结算的市场,这次没法核对")
    else:
        record("PASS", "结算状态字段", f"{len(pending)} 个在结算的市场都能读到状态")


def check_books_by_asset(sample):
    """批量盘口按 asset_id 对齐(不能按下标):每条都要带 asset_id,且都在请求的集合里。"""
    tokens = [t["token_id"] for m in sample[:10] for t in m.get("tokens", [])][:20]
    resp = http_post(
        f"{POLYMARKET_HOST}/books",
        json=[{"token_id": t} for t in tokens],
        timeout=30,
    )
    resp.raise_for_status()
    books = resp.json()
    ids = [b.get("asset_id") for b in books if isinstance(b, dict)]
    if not ids or any(i not in tokens for i in ids):
        record(
            "FAIL",
            "批量盘口",
            "返回的盘口缺 asset_id 或不在请求里,按 asset_id 对齐会失效",
        )
    else:
        record(
            "PASS", "批量盘口", f"请求 {len(tokens)} 个,回 {len(ids)} 个,都带 asset_id"
        )


def check_precise_reward(sample):
    """单市场奖励接口要能读出 rate_per_day(Step3 和发现阶段都按它判奖励够不够)。"""
    cid = sample[0]["condition_id"]
    raw = PolymarketAPI.get_rewards_for_market(cid)
    rate = sum(
        rc.get("rate_per_day", 0) for rd in raw for rc in rd.get("rewards_config", [])
    )
    if rate > 0:
        record("PASS", "单市场奖励", f"{cid[:10]} 每日奖励 {rate}")
    else:
        record(
            "FAIL",
            "单市场奖励",
            f"{cid[:10]} 读不出 rate_per_day(返回 {str(raw)[:80]})",
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--proxy", default=None, help="走这个代理(默认用环境变量 HTTPS_PROXY)"
    )
    args = ap.parse_args()
    t0 = time.time()
    with use_proxy(args.proxy):
        checks = [check_category_tags, check_resolution_field]
        sample = PolymarketAPI.get_rewards_markets(max_pages=1)[:50]
        if len(sample) < 50:
            record("FAIL", "奖励市场列表", f"首页只拿到 {len(sample)} 个")
        for fn in checks:
            try:
                fn()
            except Exception as e:
                record("FAIL", fn.__name__, f"出错: {e}")
        if sample:
            gamma = {}
            for fn in (check_gamma_batch, check_books_by_asset, check_precise_reward):
                try:
                    out = fn(sample)
                    if fn is check_gamma_batch:
                        gamma = out
                except Exception as e:
                    record("FAIL", fn.__name__, f"出错: {e}")
            report_event_level_drift(sample, gamma)
    fails = [r for r in results if r[0] == "FAIL"]
    print(
        f"\n接口自检:{len(results)} 项,FAIL {len(fails)},"
        f"WARN {sum(r[0] == 'WARN' for r in results)},耗时 {time.time() - t0:.0f}s"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
