# 历史页服务端分页 —— 修复动作过多时页面卡死

日期:2026-07-09
状态:设计定稿,待实现

## 问题

点「历史」页浏览器无响应(主线程卡死)。根因:`/api/actions`(`web/routes.py:882` → `db.get_actions` `models/database.py:593`)**不限行、不分页**,历史页一打开(`history.html:124` 的 `refreshActions()`)就把**有史以来所有动作**一次拉回,再用 `rows.map(...).join('')` + `innerHTML` **一次性塞进 DOM**。

活跃的号 `actions` 表会长到几万行(实测某天气号 6 周就 2673 行,其中 `cancel_remainder` 2548、`gap_skip` 68;全品类+高并发的号只会多得多),一次渲染几万行 DOM 直接把浏览器噎死。

后端不是卡点:`_enrich_rows` 走 `ensure_market_meta`,单次 Gamma 补名封顶 50(`engine/market_links.py:57`)且负缓存。卡的是前端全量渲染。

## 方案:服务端分页(只修展示,不动记录)

`actions` 记录逻辑一律不动(`cancel_remainder` 照记——它是过真的诊断信号)。只把「一次全渲染」改成「按页拉、按页渲染」。

### 后端

**1. `db.get_actions` 加 `limit`/`offset`(向后兼容)**

签名:`get_actions(wallet=None, start=None, end=None, action_types=None, limit=None, offset=0)`。

`limit is None` → **不加** `LIMIT`/`OFFSET` 子句(保持现状)。这是必须的向后兼容点:`get_actions` 还有第二个调用者 `engine/monitor.py:103`(水位播种,取 `max(created_at)` 需全量),以及现有无 limit 的单测。只有传了 `limit` 才追加 `LIMIT ? OFFSET ?`。

WHERE 与排序(`created_at DESC, id DESC`)不变。

**2. 新增 `db.count_actions`**

`count_actions(wallet=None, start=None, end=None, action_types=None) -> int`:复用与 `get_actions` **完全相同**的 WHERE 构造,执行 `SELECT COUNT(*) FROM actions WHERE ...`,返回总数。供前端算总页数。

**3. `/api/actions` 改为分页**

`web/routes.py` `api_get_actions`:
- 新增查询参数 `page`(默认 1,下界钳 1)、`page_size`(默认 100,钳到合理上界如 [1, 500])。
- `offset = (page - 1) * page_size`。
- `total = db.count_actions(wallet, start, end, action_types)`。
- `rows = db.get_actions(wallet, start, end, action_types, limit=page_size, offset=offset)`;`_enrich_rows(rows, "market_id")` 照旧(每页 ≤ page_size 行)。
- 返回 **`{"rows": rows, "total": total, "page": page, "page_size": page_size}`**(原为裸数组 `jsonify(rows)`;唯一消费者是 history.html,一并改)。

### 前端(`web/templates/history.html`)

**4. 分页控件 + 按页渲染**

- 表格下方加控件:`上一页` / `下一页` 按钮 + 文本「第 X / 共 Y 页(共 N 条)」。第 1 页禁用「上一页」,末页禁用「下一页」;0 条时隐藏控件、显示「暂无动作记录」。
- JS 维护 `currentPage`。`refreshActions()` 读筛选 + `currentPage`,请求 `/api/actions?...&page=&page_size=100`,用返回的 `data.rows` 渲染当前页、用 `data.total` 算总页数 `Math.ceil(total/page_size)`。
- 换筛选(钱包/动作/日期任一 change)→ `currentPage = 1` 后 `refreshActions()`;点「上一页/下一页」→ 调整 `currentPage` 后 `refreshActions()`。
- 每页 100 行,渲染快、不再卡。渲染循环、`escapeHtml`、`marketCell`、`ACTION_LABELS` 等原样。

## 不做

- 不动 actions 的记录逻辑(不少记 `cancel_remainder`,不新建表)。
- 不动 `_enrich_rows` / `ensure_market_meta`。
- 不优化 `monitor.py:103` 的全量加载(靠 `limit=None` 默认原样保留;它启动时跑一次、非卡点)。
- 不加 DB 索引(单用户本地 SQLite,几万行的 `ORDER BY created_at DESC` + 浅分页足够快;人工浏览不会翻到极深页)。
- 不做无限滚动 / 虚拟列表(YAGNI,分页够用)。

## 权衡

- OFFSET 分页在极深页(如第 500 页)要扫过 offset 行,理论上 O(offset)。单用户本地库、人工浏览基本不会翻那么深,几万行也毫秒级,可接受;真要找具体记录用现有 动作/日期/钱包 筛选先缩小。
- 响应体从裸数组变成对象:一次性 breaking change,但 `/api/actions` 唯一消费者是 history.html,同一次改到位;后端契约测试同步更新。

## 测试

`tests/test_database.py`:
1. `get_actions(limit=2)` 返回最近 2 条、顺序正确(`created_at DESC`)。
2. `get_actions(limit=2, offset=2)` 返回第 3~4 条(与不分页全量的对应切片一致)。
3. `get_actions()`(无 limit)仍返回全部——向后兼容(现有用例即覆盖,保持绿)。
4. `count_actions()` 及带 wallet/type/time 筛选时计数与 `len(get_actions(同筛选))` 一致。

`tests/test_settings_routes.py` 或历史路由测试(若有;否则新建 `tests/test_history_routes.py`):
5. `/api/actions?page=1&page_size=2` 返回 `{rows,total,page,page_size}`,`len(rows)==2`、`total==全部条数`。
6. `/api/actions?page=2&page_size=2` 返回第 2 页切片。
7. 越界 `page`(超出总页数)返回空 `rows`、`total` 不变(不报错)。

前端无 JS 单测(仓库惯例),靠人工走查 + `verify`:开历史页不卡、翻页正常、筛选后回第 1 页。

## 验收

- 全部单测绿。
- 人工:在有较多动作的库上开历史页,秒开不卡;上一页/下一页可翻;按「挂买单」筛选后回第 1 页并能翻页找到具体挂单记录。
