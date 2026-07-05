# 发现阶段只查「用得上」的品类 — 设计

日期：2026-07-05

## 现状与动机

`MarketScanner.discover_candidates`(`engine/scanner.py`)每次发现都对**整份 14 个 curated 品类**逐个查一次 `get_rewards_markets(tag_slug=slug)`，用来给每条市场打分类 tags。实测这是「点击启动 → 市场发现刷出来」慢的一大段：加上全量 `get_rewards_markets()`（5 页串行）和每市场精确奖励，一轮发现要 30+ 次网络，全走烂代理（约 30% 连接超时 + 重试），前面几十秒进度条不动。

但对只勾了少数品类的钱包（当前配置只勾 `weather`），这 14 次里绝大多数与筛选无关：

- 它们喂给 `market_wanted` 的 tags，只有「被某模板 included 的品类」才影响放行；
- 其余 tag 查询纯粹为了（a）算配置页品类计数 `last_catalog`、（b）在有模板收「其他」时判定「其他」。

因此可以只查**实际用得上**的品类，砍掉其余，发现阶段网络显著变少，且**不改变任何下单结果**。

## 正确性约束（不可违背）

`engine/categories.py` 的 `market_wanted(tags, included, include_other)`：命中 `included` 即放行；否则「`include_other` 且 tags 为空」也放行。**空 tags 恒指「无 curated 标签」**。所以：

- 若**任一启用模板** `include_other=True`：必须查**全部 14 个** slug。否则没查的品类会塌缩成空 tags，被误判成「其他」放进来 —— 过度纳入、改变行为。
- 若**没有任何模板** `include_other=True`：只需查 `included_union(templates)`。没被任何模板勾、又没人收「其他」的市场，`market_wanted` 本就丢弃，其精确 tags 无所谓。

## 方案

### `discover_candidates` 内的 `slugs_needed`

在现有 `union = included_union(templates)` / `inc_other = any_include_other(templates)` 之后计算：

```python
slugs_needed = set(CATALOG_SLUGS) if inc_other else (union & set(CATALOG_SLUGS))
```

- `union & CATALOG_SLUGS`：只取并集里属于 curated 名单的 slug（防脏数据；非 curated 的 included 值无对应 tag 端点，忽略）。
- 把原先 `_parallel_map(_tag_slug, CATALOG_SLUGS)` 改为 `_parallel_map(_tag_slug, slugs_needed)`。
- `tag_pool(full, category_ids, slugs_needed)`：打标签只用查到的 slug 集（与 `market_wanted` 一致）。
- `slugs_needed` 为空集（无勾选品类且不收其他，池必空）：跳过 tag 查询，`category_ids = {}`。

并发、代理隔离、逐 slug 容错（失败记空集）等现有逻辑不变，仅缩小 slug 集合。

### `last_catalog`（配置页品类计数）

`count_by_category` 要算正确的全 14 计数和「其他」数，必须有全 14 的 tag 集。只查子集时算不出 → **这轮不覆盖 `last_catalog`**：

- 只有当 `slugs_needed == set(CATALOG_SLUGS)`（即查了全 14，`inc_other` 为真那支）时，才 `self.last_catalog = self._catalog_payload(full, category_ids)`；
- 否则不设 `last_catalog`。`MarketScanner` 每轮由 `_scan_with_status` 新建（`engine/manager.py:857`），属性不设即为无，`getattr(scanner, "last_catalog", None)` 取到 `None`；`_scan_with_status`（`manager.py:884` 一带）本就是 `catalog = getattr(scanner, "last_catalog", None); if catalog: self._persist_catalog(catalog)`，天然跳过 → 保留上一份缓存。

配置页计数继续走既有链路：内存缓存（10 分钟）→ 本地库 → 手动「刷新」按钮（`category_catalog(refresh=True)` → `category_counts`，仍查全 14，**不改**）。

### 可见代价

纯 weather、不勾「其他」的配置下，发现阶段不再顺带刷品类计数。**首次全新启动、且从未手动刷过**时，配置页品类列表先显示「无计数」（静态清单），点一次「刷新」即有。此后有缓存则正常。用户已确认接受。

## 影响面

| 文件 | 改动 |
|---|---|
| `engine/scanner.py` | `discover_candidates`：算 `slugs_needed`；tag 查询与 `tag_pool` 改用它；仅在查了全 14 时建 `last_catalog` |
| `tests/test_scanner.py` | 新增用例（见下） |

**不动**：`category_counts`（手动刷新路径，永远查全 14）、`filter_for_template`、`engine/categories.py` 纯函数、`manager.py`（`_scan_with_status` 现有 `if catalog:` 已兼容）。

## 测试（`tests/test_scanner.py`）

沿用现有 `fake_rewards`（按 `tag_slug` 返回各 slug 的市场）/ mock API 风格：

1. **只勾单品类 → 只查该 slug**：模板 `included_categories=["weather"]`、`include_other=False`。断言 `discover_candidates` 只对 `weather` 调了 `get_rewards_markets(tag_slug=...)`（其余 slug 未被查询），且结果池里 weather 市场带 `tags=["weather"]`、非 weather 市场未混入。
2. **勾了「其他」→ 查全 14**：任一模板 `include_other=True`。断言对全部 `CATALOG_SLUGS` 都发起了 tag 查询，且非 curated 市场（空 tags）被正确当「其他」纳入。
3. **子集时不覆盖 `last_catalog`**：只勾单品类那轮后，`getattr(scanner, "last_catalog", None)` 为 None（或未更新）；查全 14 那轮后 `last_catalog` 有完整计数。
4. **多钱包并集**：两个模板分别勾 `weather` / `soccer`、均不收其他 → 只查 `{weather, soccer}`，其余不查。

统计现有绿数为基线，新增用例后应全绿。

## 验收标准

- 只勾 N 个品类、不收「其他」时，发现阶段 tag 查询次数 = N（而非固定 14）；下单/eligible 结果与改前逐条一致。
- 有模板收「其他」时，tag 查询仍为全 14，「其他」判定与计数不变。
- 配置页品类计数：有缓存/本地快照时正常显示；手动「刷新」正常联网重算。

## 非目标（YAGNI）

- 不把 14 品类计数移到后台异步刷（仍是 13 次浪费网络，只是不阻塞；本方案直接不查更省，配置页有手动刷新兜底）。
- 不改 `category_counts` 手动刷新路径。
- 不动全量 `get_rewards_markets()` 分页与每市场精确奖励（`get_rewards_for_market`：已验证批量值 ≠ 精确值、且精确值参与奖励门槛 gate，不能删 —— 另议）。
