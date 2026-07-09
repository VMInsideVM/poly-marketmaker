# 按品类分页采集 —— 修复低奖励品类(天气)候选偏少

日期:2026-07-09
状态:设计定稿,待实现

## 问题

天气号候选一直偏少。根因是采集器的候选池被「全品类前 500」卡死。

`engine/scanner.py` `discover_candidates` 的现有流程:

1. `full = self.api.get_rewards_markets()` —— **不带品类**、按 `rate_per_day` 降序、`max_pages=5`(500 条封顶)。
2. 每个需要的品类 `_tag_slug(slug)` → `get_rewards_markets(tag_slug=slug)`,**只取 condition_id 集合**(`scanner.py:130`)。
3. `tag_pool(full, category_ids, slugs_needed)` —— **只遍历 `full`** 给市场打标签。

### 两层叠加截断(实盘日志坐实)

2026-07-05 市场日志,某轮 `union=1`(仅天气)、`include_other=False`,整轮恰好两段抓取,两段都打到 `Page 5: got 100 (total 500)`:

- 段一 `full`(全品类):total 500 触顶 → `next_cursor` 从没到底 → **全品类奖励市场总数 > 500**。
- 段二 `weather` tag:total 500 触顶 → **天气品类自己就 ≥ 500**。
- 而 `discover_candidates` 最终只产出 205~247 个天气候选。

**Cap B(主因):** `tag_pool` 只在 `full` 上打标签。`full` 是全品类前 500,天气奖励普遍低、绝大多数排在全局 500 名外,`full` 里根本没有它们;`_tag_slug('weather')` 明明查到了这些市场,但返回值只被拿去给 `full` 里的市场打标签,不在 `full` 里的天气市场被直接丢弃。

**Cap A(次因):** 天气 tag 查询本身也被 `max_pages=5` 截断,天气内部排 500 名外的那截也丢。

两者都源自 `max_pages=5` + 「tag 只覆盖 `full`」的设计。与天气奖励高低无关:只要品类总数 + 全局总数都 > 500,任何低奖励品类都会被 Cap B 吞。

### 现有测试为何没抓到

`tests/test_scanner.py::TestFetchCandidatesCategoryWiring::test_whitelist_keeps_only_included_union` 里,tag 查询返回的 A/B **恰好也在 `full` 里**,所以「tag 查到、但不在 `full`」这条 bug 路径从没被覆盖。

## 方案:候选池由「各需要品类的 tag 抓取」并集组成

不再拿全品类前 500 去交集,而是把各品类 tag 抓取的市场记录本身**并进候选池**。`full` 只在有号勾了「其他」时才需要(去捞无 curated 标签的市场)。每品类抓取页数上限**可调,默认 20 页**。

### 改动一:`scanner.py` `_tag_slug` 返回整条记录

`get_rewards_markets(tag_slug=X)` 走的是同一个 `/rewards/markets/multi` 端点,返回的市场对象结构与 `full` 完全一致(含 `tokens`/`rewards_config`/`question`/`market_slug`/`event_slug`)。改为保留 rows,而非只留 cid。失败容错不变(抛错的 slug 记空 rows)。

由 rows 另建 `category_ids = {slug: {cid…}}`(打标签 + catalog 计数仍需 cid 集合)。

### 改动二:`discover_candidates` 重建候选池

```
union, inc_other, slugs_needed = ...                     # 不变
slug_rows = dict(_parallel_map(_tag_slug, slugs_needed)) # {slug: [row…]}
category_ids = {slug: {row cid…} for slug, rows in slug_rows}

full = self.api.get_rewards_markets(max_pages=max_pages) if inc_other else []

by_cid = {}
for slug in slugs_needed:
    for m in slug_rows.get(slug, []):
        cid = m.get("condition_id", "")
        if cid and cid not in by_cid:
            by_cid[cid] = m
if inc_other:
    for m in full:                    # 补「其他」= 无 curated 标签的市场
        cid = m.get("condition_id", "")
        if cid and cid not in by_cid:
            by_cid[cid] = m

pool = tag_pool(list(by_cid.values()), category_ids, slugs_needed)
# 其后 blacklist / market_wanted / min_floor 粗筛、精确奖励、订单簿 —— 全不变
```

- `include_other=False`(天气号):**完全跳过 untagged `full` 抓取**,候选池 = 天气品类全部市场。铲掉 Cap B。
- `include_other=True`:并集 = 各品类 rows ∪ `full`,行为等价今天,但不再漏低奖励品类。
- catalog 快照守卫是 `if inc_other and slugs_needed == set(CATALOG_SLUGS)`(**显式**要求 inc_other:`slugs_needed==全14` 也可能出现在「勾满 14 品类但不勾其他」时,此时 `full=[]`,若不加 `inc_other` 会算出零计数却标 `ready:True` 污染快照——评审阶段修正);`inc_other` 时 `full` 有值,`_catalog_payload(full, category_ids)` 照常;`category_ids` 仍是 {slug: set(cid)},契约不变。

### 改动三:每品类页数上限可调(默认 20)

- `config.py` `ENGINE_DEFAULTS` 增 `"reward_scan_max_pages": 20`(扫描级全局,非 per-钱包;与 `discovery_interval_sec` 并排)。
- `scanner.discover_candidates` / `fetch_candidates` 增形参 `max_pages`,默认取 `ENGINE_DEFAULTS["reward_scan_max_pages"]`;透传给每个 `get_rewards_markets(..., max_pages=…)`(tag 查询与 `full` 都用)。
- `scanner` 本身**不读** `db.get_settings()`(否则现有用 MagicMock db 的 scanner 测试会炸);由 `manager` 读设置后透传,沿用「manager 读设置、scanner 收参数」的既有分工。

### 改动四:`category_counts` 一并用同一上限

配置页品类计数(手动刷新)现也是 5 页 500 顶,低奖励品类少算。

- `scanner.category_counts(catalog, max_pages=…)` 增形参,透传给它内部的 `get_rewards_markets`(`full` 与逐 slug)。
- `manager._compute_category_catalog`(`manager.py:650`)读设置后透传。

### 改动五:`manager` 两处透传

- `_scan_with_status` → `scanner.fetch_candidates(templates, …, max_pages=self.db.get_settings()["reward_scan_max_pages"])`。
- `_compute_category_catalog` → `scanner.category_counts(CATEGORY_CATALOG, max_pages=self.db.get_settings()["reward_scan_max_pages"])`。

### 改动六:配置页加输入框

`web/templates/config.html` 的「引擎参数(全局)」表单加一个数字输入:

```html
<div class="form-group">
    <label>每品类扫描页数上限 (每页 100，默认 20)</label>
    <input type="number" name="reward_scan_max_pages" step="1">
</div>
```

引擎表单 JS 通用:加载按 name 回填(`config.html:338`)、提交按 name 序列化(`:417`),**零 JS 改动**。POST 路由按 `ENGINE_DEFAULTS` 白名单收键、GET 合并 `get_settings()`,**routes.py 不改**。

> ⚠️ 该文件含中文,由主 agent 直接 Write(subagent 易写别字 + 加 BOM);写后 `node --check` 不适用(是 HTML),改为 grep 校验中文正确 + 无 BOM。

## 不做

- 不改 `get_rewards_markets` 自身的 `max_pages=5` 默认(其它调用方不受影响)。
- 不给品类抓取做「无限翻到底」(用户选了可调上限,20 页=2000/品类,够覆盖天气当前规模又不失控)。
- 不动 `filter_for_template`、离场、下单等下游逻辑。

## 权衡

- `include_other=True` 时会 full(≤2000)+ 各品类(各 ≤2000)都抓,比现在重;但仅在 4h 一次的发现阶段、并发封顶 4、且每品类抓到自身 `next_cursor` 到底即停(多数品类 < 5 页),实际远达不到上限。可调上限即失控保险。
- 品类总数若超过 `reward_scan_max_pages × 100` 仍会截断 —— 这是用户接受的可调取舍,调大上限即可。

## 测试

`tests/test_scanner.py`:

1. **bug 复现(核心):** tag 查询返回天气市场 `W`,`full` 不含 `W`(或 `include_other=False` 时不抓 full)→ 断言 `W` 在候选池。现状漏,修后在。
2. **dedup:** 某 cid 同时在两个品类 tag(或 tag 与 full)→ 池里只出现一次。
3. **`include_other=False` 不抓 untagged full:** 断言 `get_rewards_markets` 无 `tag_slug=None` 的调用。
4. **`include_other=True` 仍收无标签市场:** 等价现有 `test_include_other_keeps_untagged`,`full` 独有的 `C` 落「其他」。
5. **单 slug 失败容错不变:** 等价现有 `test_slug_query_failure_tolerated`。
6. **`max_pages` 透传:** 断言每个 `get_rewards_markets` 调用带 `max_pages=<传入值>`。
7. **`category_counts` 透传 `max_pages`。**

契约测试(现有 `/api/settings` 契约用例):补 `reward_scan_max_pages` 可经 POST 存、GET 回。

## 验收

- 全部单测绿。
- bug 复现用例:修前红、修后绿。
- 实盘(用户环境):天气号候选数明显上升,发现阶段日志里天气 tag 抓取翻页超过 5 页(至 `next_cursor` 到底或 20 页)。
