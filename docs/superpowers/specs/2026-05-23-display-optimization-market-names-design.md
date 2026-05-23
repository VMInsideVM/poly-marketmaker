# 设计：订单管理/监控页展示优化（拆分滚动区 + 市场名称/超链接/复制）

日期：2026-05-23

> 这是用户本批需求拆出的**第一个**子项目（展示优化）。第二个子项目「全局黑名单」单独走 spec→plan，不在本文件范围内。

## 背景与问题

用户反馈三类展示问题：

1. **订单管理页**（`web/templates/orders.html`）：「当前挂单」和「当前持仓」上下纵向堆叠。挂单一多，要往下滚很远才能看到持仓。
2. **监控状态页**（`web/templates/logs.html`）：「监控状态」和「操作记录」同样上下堆叠、互相挤占；启动引擎后监控状态行很多，要往下滚才能看到操作记录。
3. **市场标识不友好**：多处只显示 condition_id（且是截断的），看不到市场名称，也无法跳转到 Polymarket 交易页，想复制完整 condition_id 也没有入口。

### 当前数据现状（决定方案的硬约束）

- **当前挂单** `/api/orders`（CLOB `get_open_orders`，`web/routes.py:422`）：每条只有 `market`（= condition_id 十六进制）、`asset_id`（token_id）、`outcome`。**无人名、无 slug**。orders.html 的「市场」列现在显示的就是 `o.market`（condition_id）。
- **当前持仓** `/api/positions`（Data API `get_user_positions`，`web/routes.py:524`）：返回 `title`（人名）、`conditionId`、`avgPrice`/`curPrice`/`size`/`outcome`。已有人名，无 slug。
- **监控状态** `/api/monitor-status`（`engine/monitor_status.py` 的快照，row 的 `market` 由 `engine/monitor.py` 写为 `cid`=condition_id）：只有 condition_id。
- **操作记录** `/api/actions`（`web/routes.py:566`，读 `actions` 表）：只有 `market_id`（condition_id）。logs.html 显示成 `market_id.slice(0,8)+'...'+slice(-6)`（截断）。
- **eligible_markets 表**：存了 `market_id`(condition_id) + `market_name`(question)，但**没有 slug**；且 `save_eligible_markets` 每次扫描先 `DELETE FROM eligible_markets` 再插入，只保留最近一次扫描的市场。
- **扫描数据源**（rewards 接口，`get_rewards_markets`）：每个 market 同时带 `market_slug` 和 `event_slug`（见 `test_live.py:184-185`）。

### 已验证的链接格式（复用项目现有逻辑）

`test_live.py:335-338` 已有：

```python
market_slug = market.get("market_slug", "")
event_slug  = market.get("event_slug", "")
if market_slug:
    url = f"https://polymarket.com/market/{market_slug}"
elif event_slug:
    url = f"https://polymarket.com/event/{event_slug}"
```

本设计沿用同一格式：`market_slug` 优先 → `/market/{slug}`，否则 `event_slug` → `/event/{slug}`，都没有则不出链接。

### 结论

要让挂单/持仓/监控状态/操作记录都显示「市场名 + 可点击跳转 Polymarket + 可复制完整 condition_id」，需要一个 **condition_id → {名称, slug}** 的映射。方案选定为「本地映射为主」：扫描时把市场元信息落进一张**持久、不随扫描清空**的小表，后端据此给各接口补名称与链接；映射里没有的（极少：未扫描过的市场）回退为截断 id + 无链接。

## 方案

### 第 1 节 · 数据层：持久化市场元信息

新增表（`models/database.py` `_create_tables`）：

```sql
CREATE TABLE IF NOT EXISTS market_meta (
    condition_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    market_slug TEXT NOT NULL DEFAULT '',
    event_slug TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
```

因为是新表，`CREATE TABLE IF NOT EXISTS` 即可，老库自动创建，无需写迁移分支。

新增 DB 方法：

- `upsert_market_meta(condition_id, name, market_slug, event_slug)`：`INSERT OR REPLACE`（更新 `updated_at`）。空 condition_id 直接跳过。
- `get_market_meta() -> dict`：返回 `{condition_id: {"name":..., "market_slug":..., "event_slug":...}}`，供路由层批量补全。

scanner（`engine/scanner.py`）：在扫描循环里，对每个有效市场调用 `self.db.upsert_market_meta(condition_id, question, market.get("market_slug",""), market.get("event_slug",""))`。该表**不清空**，逐次累积，覆盖比 `eligible_markets` 广（包括已掉出最近扫描的历史市场）。

### 第 2 节 · 后端：给各接口补名称 + 链接

新增纯函数（放 `web/routes.py` 或新模块 `engine/market_links.py`，便于单测）：

```python
def market_url(meta_entry: dict) -> str:
    """从 meta 条目构造 Polymarket 链接；无 slug 返回空串。"""
    if not meta_entry:
        return ""
    ms = meta_entry.get("market_slug") or ""
    es = meta_entry.get("event_slug") or ""
    if ms:
        return f"https://polymarket.com/market/{ms}"
    if es:
        return f"https://polymarket.com/event/{es}"
    return ""


def enrich_with_market_meta(rows: list[dict], meta: dict, id_key: str) -> list[dict]:
    """给每行按 row[id_key]==condition_id 补 market_name(缺则不覆盖已有)和 market_url。
    就地修改并返回 rows。未命中则 market_url='',market_name 维持原值或空。"""
    for r in rows:
        cid = r.get(id_key, "") or ""
        entry = meta.get(cid)
        r["market_url"] = market_url(entry) if entry else ""
        if entry and not r.get("market_name"):
            r["market_name"] = entry.get("name", "")
    return rows
```

各接口接入（每个接口取一次 `meta = db.get_market_meta()`）：

- `/api/orders`（`web/routes.py:422`）：每条 order 结果里**保留** `market`(condition_id)，新增 `market_name` + `market_url`（用 `enrich_with_market_meta(result, meta, "market")`）。
- `/api/positions`（`web/routes.py:524`）：名称仍用 Data API 的 `title`（已填进 `market_name`）；**新增** `condition_id`（= `p.get("conditionId","")`）和 `market_url`（按 conditionId 查 meta）。`market_name` 已有，`enrich` 不覆盖。
- `/api/actions`（`web/routes.py:566`）：结果每行**保留** `market_id`，新增 `market_name` + `market_url`（`enrich_with_market_meta(rows, meta, "market_id")`）。
- `/api/monitor-status`（`web/routes.py:196`）：在**路由层**对 `get_snapshot()` 返回的 `rows` 调 `enrich_with_market_meta(snap["rows"], meta, "market")`，补 `market_name` + `market_url`，保留 `market`。`engine/monitor.py` / `engine/monitor_status.py` **不改动**——交易/监控代码不感知市场名。

### 第 3 节 · 前端

共用 JS 助手（放 `web/templates/base.html` 的全局 `<script>`，供各页调用）：

```js
function marketCell(name, conditionId, url) {
  const label = name || (conditionId
    ? conditionId.slice(0,8) + '...' + conditionId.slice(-6) : '');
  const safe = escapeHtmlOrText(label);            // 复用/内联转义
  const link = url
    ? `<a href="${url}" target="_blank" rel="noopener">${safe}</a>` : safe;
  const copyBtn = conditionId
    ? ` <button class="btn btn-xs" title="复制完整 condition ID"
         onclick="copyCid('${conditionId}')">📋</button>` : '';
  return `<span title="${conditionId||''}">${link}${copyBtn}</span>`;
}

function copyCid(cid) {
  const done = () => toastOrAlert('已复制 condition ID');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(cid).then(done).catch(() => fallbackCopy(cid, done));
  } else { fallbackCopy(cid, done); }
}
// fallbackCopy: 临时 textarea + document.execCommand('copy')
```

> 备注：本地 `http://127.0.0.1:5000` 属浏览器「安全上下文」，`navigator.clipboard` 可用；仍保留 textarea + `execCommand` 回退。复制成功用轻量提示（沿用现有 alert 或一个简单 toast，实现时取最省的）。

页面改动：

- **orders.html**：
  - 「当前挂单」table 和「当前持仓」table 各包一层限高滚动容器：`<div class="table-scroll">…</div>`，CSS `max-height` + `overflow:auto`（建议 ~42vh，实现时按实际效果微调；表头用 `position:sticky; top:0` 固定）。两者始终可见。
  - 挂单的「市场」列 `o.market||''` 改为 `marketCell(o.market_name, o.market, o.market_url)`。
  - 持仓的「市场」列 `p.market_name` 改为 `marketCell(p.market_name, p.condition_id, p.market_url)`。
- **logs.html**：
  - 「监控状态」table 和「操作记录」table 各包同样的限高滚动容器，表头 sticky，两者始终可见。
  - 监控状态「市场」列 `r.market` 改为 `marketCell(r.market_name, r.market, r.market_url)`。
  - 操作记录「市场」列（现在的截断 `a.market_id`）改为 `marketCell(a.market_name, a.market_id, a.market_url)`。
- **dashboard.html（可选，保持一致）**：eligible 表「市场名称」列由 `m.market_name`（`title=m.market_id`）改为 `marketCell(m.market_name, m.market_id, m.market_url)`。需要 `/api/eligible` 一并补 `market_url`（同样用 meta 映射）。此项为一致性增强，可在实现时按需取舍。

CSS：在 `base.html` 现有样式里加 `.table-scroll{max-height:42vh;overflow:auto;}`、`.data-table thead th{position:sticky;top:0;background:…;}`、`.btn-xs{…}`。

### 第 4 节 · 错误处理与边界

- meta 映射未命中：`market_url=''`（前端不出链接），名称回退为截断 condition_id；复制按钮仍可复制完整 id。
- `get_market_meta()` 失败/为空：各接口照常返回，只是没有名称/链接（不应抛错中断接口）。
- slug 含特殊字符：rewards 返回的 slug 本身是 URL 安全的；前端拼接到 `href` 时按普通文本处理即可（slug 不含引号）。
- 复制：无 clipboard API 时走 textarea 回退；两者都失败则提示用户手动从 `title` 悬浮框复制。

## 测试

- `tests/test_database.py`：
  - `upsert_market_meta` 后 `get_market_meta` 能取回；同 condition_id 再次 upsert 会更新 name/slug（主键去重，不产生重复行）；空 condition_id 被跳过。
- `tests/test_scanner.py`：扫描后 `market_meta` 含被扫描市场的 name + market_slug + event_slug（用现有 scanner 测试的 mock 方式，断言 `db.upsert_market_meta` 被以正确参数调用，或对内存 DB 断言取回结果）。
- 新建 `tests/test_market_links.py`：纯函数单测——
  - `market_url`：有 `market_slug` → `/market/...`；无 market_slug 有 `event_slug` → `/event/...`；都无 → `''`；空 entry → `''`。
  - `enrich_with_market_meta`：命中补 `market_name`+`market_url`；未命中 `market_url=''`；已有 `market_name`（如持仓 title）不被覆盖；`id_key` 分别为 `market` / `market_id` 时都正确。
- 前端模板（限高滚动、marketCell、复制按钮）：项目无模板单测，手动验证。

## 不在本子项目范围

- 全局黑名单（按 condition_id 拉黑、引擎挂单拦截、黑名单管理界面、当前挂单行的「一键加入黑名单」按钮）——下一个 spec。
