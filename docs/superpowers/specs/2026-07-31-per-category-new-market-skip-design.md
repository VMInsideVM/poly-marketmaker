# 新市场保护按品类勾选 + 配置页钱包余额列 设计 / spec

> 日期：2026-07-31　　状态：已批准，待写实现计划

两件独立的改动合并成一个 spec：把「跳过新建市场」从整模板一刀切改成按做市品类勾选启用，
以及配置页钱包表加一列余额。

## 一、新市场保护按品类勾选

### 1.1 背景与目标

`skip_new_markets` / `new_market_hours`（2026-07-27 上线，见
`2026-07-27-skip-new-markets-and-pnl-start-design.md`）现在是整模板一刀切：开了就对该模板做市的
所有品类生效。但不同品类的新市场风险差别很大：体育这类市场天生创建即交易、生命周期以小时计，
一律等 24 小时等于不做；而政治、加密这类新市场盘口薄、价格未定型，正是保护要针对的。

目标：保留一个总开关和一个保护期小时数，但让用户勾选**这条规则对哪些做市品类生效**。

### 1.2 配置（模板级，两个新键）

在 `config.py` `TEMPLATE_DEFAULTS` 里加，与现有的 `included_categories` / `include_other` 一一对仗：

| 键 | 类型 | 默认值 |
|---|---|---|
| `skip_new_categories` | list[str] | `[c["slug"] for c in CATEGORY_CATALOG]`（全部 14 个） |
| `skip_new_other` | bool | `True` |

默认值取自 `CATEGORY_CATALOG` 而不是 `CATALOG_SLUGS`：后者是 frozenset，顺序不稳定，
存进 DB 和进去重键都需要确定的序（`DEFAULT_INCLUDED_CATEGORIES` 同样是从 `CATEGORY_CATALOG` 推导的）。

`skip_new_markets`（总开关）与 `new_market_hours`（小时数）保持原样，仍是每模板一个值。
需求是「勾选启不启用」，不是「每类各设一个小时数」，后者不做。

**默认「全部品类都保护」是升级兼容的关键。** `Database.get_template` 把 `TEMPLATE_DEFAULTS`
合并进该模板存储的覆盖键，老模板没存这两个键就自动拿默认值，判定结果与改动前逐字相同。
已经开了开关的用户不会突然少保护或多保护，没开开关的用户更不受影响。

`web/routes.py` 的 `/api/settings`、`/api/templates/<id>` 按 `TEMPLATE_DEFAULTS` 白名单自动存取，
后端路由无需逐键改。**前端 checkbox 必须在 collect 函数里单独收**，白名单只负责过滤，不会替你
从 DOM 里读出 checkbox 的值。这是配置链路上踩过不止一次的坑。

### 1.3 判定口径：任一命中就保护

一个市场可以同时命中多个 curated 品类（`tag_pool` 给每条市场打上它命中的全部 slug）。口径定为
**任一命中**：市场的任意一个品类在保护名单里，保护就生效。空 `tags`（= 不属于任何 curated 品类，
即「其他/未分类」）由 `skip_new_other` 单独管。

这与做市品类白名单 `market_wanted(tags, included, include_other)` 的逻辑**逐字相同**：命中任一
slug 为真，或空 tags 且收「其他」为真。所以抽出一个语义中立的名字，两处共用一份实现，避免两个
口径日后各自漂移（与 `has_cliff_below` 同时服务下单和 Step3 是同一个模式）：

```python
def market_in_categories(tags, slugs, include_untagged: bool) -> bool:
    """市场是否落在给定品类集合内。命中任一 slug -> True；
    空 tags(= 无 curated 标签,即「其他」)且 include_untagged -> True。"""

def market_wanted(tags, included, include_other) -> bool:   # 保留原名，一行封装
    return market_in_categories(tags, included, include_other)
```

现有 `market_wanted` 的调用点与测试一律不动。

### 1.4 判定落在两处（沿用原有的两处结构）

原设计把新市场判定放在共享的发现阶段和每模板的 `prefilter_for_template` 各一次，本次改动
保持这个结构，只是两处都加上品类条件。

**a. `prefilter_for_template`（`engine/scanner.py`）**

原判定：

```python
if skip_new and new_hours:
    age = market_age_hours(market.get("created_at", ""), now)
    if age is not None and age < new_hours:
        continue
```

加一个前置条件。`skip_cats` / `skip_other` 和 `skip_new` / `new_hours` 一样在循环外从 template 取一次，
`tags` 就是循环里 `market_wanted` 刚读过的 `market.get("tags", [])`：

```python
skip_cats = set(template.get("skip_new_categories", []) or [])
skip_other = bool(template.get("skip_new_other", False))
...
if skip_new and new_hours and market_in_categories(tags, skip_cats, skip_other):
    age = market_age_hours(market.get("created_at", ""), now)
    if age is not None and age < new_hours:
        continue
```

**b. 发现阶段 `loosest_new_market_hours`（`engine/scanner.py`）**

发现阶段是钱包无关的共享阶段，安全性论证是「只有**每个**模板都会因太新而排除它，才能在这里
排除」。品类条件进来后，「会排除」多了一个判断维度，函数从「整轮算一个门槛」变成「按市场算门槛」：

```python
def loosest_new_market_hours(templates, tags) -> float:
    """发现阶段可安全排除该市场的门槛(小时)。任一模板不会因「太新」排除它 -> 0。"""
    hours = []
    for t in templates:
        if not t.get("skip_new_markets"):
            return 0.0
        if not market_in_categories(
            tags,
            t.get("skip_new_categories") or [],
            bool(t.get("skip_new_other")),
        ):
            return 0.0
        hours.append(_new_market_hours(t))
    return min(hours) if hours else 0.0
```

调用点从 `discover_candidates` 的循环外挪进循环内，传该市场的 `tags`。纯计算不发网络，
候选池 2000 条 × 模板数（实际 1 到 3 个）× 一次集合求交，微秒级，对首单耗时无影响。

`new_market_hours = 0` 视同不筛的既有语义不变（`_new_market_hours` 已把非数字/负数/缺失归 0）。
`created_at` 取不到时 fail-open 保留，两处都不变。

**c. `_active_templates` 去重键（`engine/manager.py`）**

去重键的既有契约是「须含采集器实际用到的每个维度」。发现阶段现在要读这两个新键，它们就得进键：

```python
tuple(sorted(tmpl.get("skip_new_categories", []) or [])),
bool(tmpl.get("skip_new_other", False)),
```

漏掉的话，只有保护品类不同的两个模板会被去重成一个，另一个模板的门槛没进并集，
它想要的市场会在发现阶段被误剔。

### 1.5 配置页：品类区改双列表格

把「跳过新建市场」开关和「新市场保护期（小时）」两个 form-group 从上面的「策略参数」区**挪到**
品类区标题下，一个功能的配置散在页面两处不好理解。品类列表从一排 inline label 改成表格：

```
做市品类（只做勾选的）                    刷新计数 · 计数更新于 3 分钟前
☑ 跳过新建市场    保护期 [ 24 ] 小时

 品类            计数    做市    跳过新市场
 政治            123      ☑        ☑
 经济             45      ☑        ☐
 体育              0      ☐        ·（置灰）
 其他/未分类      78      ☑        ☐
```

- 「其他/未分类」从表格外的独立 label 变成表格最后一行。
- 「做市」未勾选 → 该行「跳过新市场」框 `disabled` 置灰；总开关未勾选 → 整列置灰。
  两种情况下值都照常收集保存，取消勾选再勾回来不丢配置。
- `renderCategories` 多收两个入参（保护名单、是否保护其他），`loadStrategy` 传进去。
- **`refreshCategories` 必须把新的两组勾选一起保住**：它现在的做法是重渲染前先把
  `included_categories` 和 `include_other` 的当前勾选读出来再传回去，新加的两组不照做的话，
  用户点一次「刷新计数」保护勾选就被打回默认值。
- 收集函数加 `data.skip_new_categories` 与 `data.skip_new_other`。

### 1.6 代价：候选池变大

缩小保护范围后，原先在发现阶段就被剔掉的新市场会重新进候选池。它们若通过档位与结算窗口门控
（`_should_price`），发现阶段就要为每条多发一次精确奖励请求（约 0.78s/条，4 小时一轮）。
这是「保护范围变小」的直接结果，不是缺陷；不缩小范围的用户（默认值）成本完全不变。

### 1.7 文档

`CLAUDE.md` 与 `README.md` 里描述这个开关的段落要改成按品类：生效窗口的说法从「所有模板都开启
本开关时」变成「所有模板都开启且都保护该品类时」。

## 二、配置页钱包表加余额列

`/api/wallets` **已经在查并返回 `balance`**：引擎在跑就用引擎自己的 api 实例，没跑就用
`_get_cached_api` 现开一个。配置页每次加载都已经付了这个网络开销，只是前端把数字扔掉了。

所以是纯前端改动：`wallet-config-table` 表头在「地址」后插一列「余额」，`loadWallets` 渲染
`${w.balance != null ? '$' + w.balance.toFixed(2) : '-'}`，格式与 `dashboard.html` 一致。
后端零改动、零额外请求。

## 三、测试

`tests/test_categories.py`
- `market_in_categories`：任一命中为真、空 tags 且收其他为真、空 tags 不收其他为假、都不命中为假。
- `market_wanted` 现有用例保持全绿（验证一行封装没改口径）。

`tests/test_scanner.py`
- `loosest_new_market_hours(templates, tags)` 新签名：全模板都保护该品类取最小 N、某模板开关没开返回 0、
  某模板不保护该品类返回 0、空模板列表返回 0、`tags` 为空时走 `skip_new_other`。
- `discover_candidates` 集成：新市场落在保护品类里被剔出候选池；落在未保护品类里保留。
- `prefilter_for_template`：同一个新市场，保护品类下被剔、非保护品类下保留。

`tests/test_database.py`
- 两个新键的默认值：`skip_new_categories` 含全部 `CATALOG_SLUGS`、`skip_new_other is True`。

`tests/test_manager.py`
- `_active_templates`：只有 `skip_new_categories` 不同、或只有 `skip_new_other` 不同的两个模板不被去重。

前端没有自动化测试（项目现状），改完人工走查：切模板回填、点刷新计数、置灰联动、保存后重载。
