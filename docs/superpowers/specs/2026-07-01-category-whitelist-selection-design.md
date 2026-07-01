# 品类白名单勾选（category whitelist selection）设计 / spec

> 日期：2026-07-01
> 状态：待用户评审

## 零、背景与定位

当前品类筛选是**写死 3 项的黑名单**：`config.html` 的「排除品类」只有体育(sports)/电竞(esports)/天气(weather) 三个 checkbox，默认三个都勾上排除；扫描时把勾中的品类从池子里剔除，其余（含所有没识别到品类的市场）全保留。

用户要的是：**配置页能自己挑品类**——所有能做的品类都给个勾选项，白名单模式（只做勾选的）。

### 决策（已与用户确认）

1. **白名单**：勾中的品类才做，没勾的一律不做。
2. **品类清单来源**：内置一份 curated 干净名单 × 实时交集——代码维护约 14 个大品类，配置页只展示"当前奖励市场里真实有市场的那几个 + 市场数"。
3. **「其他/未分类」兜底勾选**：匹配不到名单里任何 curated 品类的市场（只带冷门标签，或临时取不到标签）归入「其他」；勾上才做，默认不勾。这条也是失败兜底——万一品类数据整批取不到，勾了「其他」不会整个停单。
4. **默认值延续当前行为**：默认 `included_categories` = curated 名单去掉体育/电竞/天气，`include_other = True`（即"这三类不做、其余含未分类都做"，精确复刻升级前的默认）。

### 为什么不用 Gamma event 标签

实测（2026-07-01）：CLOB 奖励端点 `get_rewards_markets(tag_slug=…)` 对各 curated 品类**可靠**——politics 19、sports 27、soccer 16、weather 19、economy 11、geopolitics 12、games 10、finance 7、elections 6、esports 6、world 4、ai 2、tech 2（当前奖励页 100 市场内的命中数），这 17 个 slug 并集覆盖 86/100，剩 14 落入「其他」。crypto/culture/science/business 当前 0（此刻无该类奖励市场，正常）。

因此**沿用现有 `partition_candidates` 的按 slug 打标签机制**（只把"排除并集"改成"包含并集"），改动最小、复用已测代码、留在工具已信任的奖励端点上。不引入 Gamma `/events` 标签这套更重、且把 Gamma 拉进关键过滤路径的机制。Gamma `/tags` 是上千项的大杂烩（含 `caitlin-clark`/`virgins`/内部 `hide-from-new` 之类噪声），不适合直接当品类清单，已排除。

## 一、数据模型变更

### 1.1 `config.py` `TEMPLATE_DEFAULTS`

- **删** `"excluded_categories": ["sports", "esports", "weather"]`。
- **加**：
  - `"included_categories": [<curated 名单去掉 sports/esports/weather 的全部 slug>]`
  - `"include_other": True`

因模板走 key/value merge（`get_template` = `TEMPLATE_DEFAULTS` + 逐键覆盖）、保存按 `k in TEMPLATE_DEFAULTS` 过滤（`web/routes.py`），新键自动持久化，**路由无需改**。

### 1.2 新常量 `CATEGORY_CATALOG`（config.py）

curated 品类名单，`slug → 中文 label`，有序（决定配置页展示顺序）。初版（按实测落定，易于日后增删）：

| slug | label |
| --- | --- |
| politics | 政治 |
| geopolitics | 地缘政治 |
| world | 国际 |
| elections | 选举 |
| economy | 经济 |
| finance | 金融 |
| crypto | 加密货币 |
| sports | 体育 |
| soccer | 足球 |
| esports | 电竞 |
| games | 游戏 |
| weather | 天气 |
| ai | 人工智能 |
| tech | 科技 |

`CATALOG_SLUGS = {c["slug"] for c in CATEGORY_CATALOG}`（打标签时的交集集合）。

「其他」不是一个 slug，是**逻辑桶**：`market.tags`（∩ curated 后）为空即属「其他」。

## 二、`engine/categories.py` 重写

黑名单三函数（`queried_categories` / `excluded_intersection` / `partition_candidates`）→ 白名单版：

```python
def included_union(templates) -> set:
    """所有模板 included_categories 的并集(= 发现阶段需向服务端查询的品类)。"""

def any_include_other(templates) -> bool:
    """是否有模板开了 include_other(决定发现阶段是否保留无 curated 标签的市场)。"""

def tag_pool(full_markets, category_ids, catalog_slugs) -> list[dict]:
    """给每条市场打 tags = 命中的 curated slug 列表(category_ids: {slug: set(cid)});
    不做删除,纯打标签。替代 partition_candidates 的打标签部分。"""

def market_wanted(tags, included, include_other) -> bool:
    """白名单判定:tags 命中 included,或(tags 为空且 include_other)。"""
```

`market_wanted` 同时用于发现阶段预筛（`included=included_union`、`include_other=any_include_other`）和每模板精筛（`included=模板的 included_categories`、`include_other=模板的 include_other`）。

## 三、`engine/scanner.py` 变更

### 3.1 `discover_candidates`

- `inter/queried`（交集/并集排除）→ `union = included_union(templates)`、`inc_other = any_include_other(templates)`。
- 逐 slug 查询改查 `union`：`for slug in union: category_ids[slug] = {cid...}`。
- `partition_candidates(full, category_ids, inter)` → `tag_pool(full, category_ids, CATALOG_SLUGS)`（只打标签，不删）。
- 预筛：进入昂贵的逐市场精确奖励拉取（`get_rewards_for_market`）**之前**，先 `if not market_wanted(market["tags"], union, inc_other): continue`。这替代原先靠 intersection 提前删除的省流作用——没被任何模板 include 的市场不做精确拉取。
- 日志 "queried N categories" 改为 included 并集大小。

### 3.2 `filter_for_template`

- `excluded = set(template["excluded_categories"])` + `if excluded & set(market["tags"]): continue`
  → `included = set(template["included_categories"])`、`inc_other = template["include_other"]`
  → `if not market_wanted(market.get("tags", []), included, inc_other): continue`（放在原 excluded 检查的同位置，其余门槛过滤/定价不变）。

## 四、`engine/manager.py` 变更

`_active_templates` 去重键（第 628-631 行）：
```python
key = (
    tuple(sorted(tmpl.get("included_categories", []) or [])),
    bool(tmpl.get("include_other", False)),
    tmpl.get("min_reward_usd", 0),
)
```

## 五、配置页

### 5.1 新路由 `GET /api/categories`（`web/routes.py`）

返回当前品类目录 + 实时市场数：
```json
{"categories": [{"slug": "politics", "label": "政治", "count": 19}, ...],
 "other_count": 14}
```
- 计算：`full = get_rewards_markets()`；对每个 curated slug 查 `get_rewards_markets(tag_slug=slug)` 取与 `full` 的交集计数；`other_count` = full 中未命中任何 curated slug 的数量。
- **短 TTL 内存缓存**（例如 600s，与 `rewards_cache_ttl_sec` 同量级）避免每次开配置页狂查 ~15 次。
- 走扫描钱包代理（`use_proxy`，与 `fetch_candidates` 一致）；任一 slug 查询失败则该项省略 count（不阻断整体）。
- 需 `manager` 已就绪（已登录）；未就绪返回空 categories + 提示。

### 5.2 `config.html`

- 删写死的 3 个 checkbox（第 101-103 行）；`#excluded-categories` 容器改名/复用为 `#included-categories`。
- 加载时 `fetch('/api/categories')` 动态渲染：对 `count ≥ 1` 的 curated 品类渲染 `<label><input type=checkbox value=slug> 中文名 (count)</label>`；末尾固定一个「其他/未分类 (other_count)」→ 对应 `include_other`。
- 回填：模板的 `included_categories` 勾上；`include_other` 勾上「其他」。**已选但当前 count=0 的品类也要渲染出来并勾上**（不能因当前无市场就丢掉用户的选择）——即渲染集合 = `{count≥1 的 curated} ∪ {模板已选的 included_categories}`。
- 保存（第 477-478 行）：`data.included_categories` = 勾中的（排除「其他」）checkbox value 数组；`data.include_other` = 「其他」是否勾中。
- 品类目录取不到时兜底：展示全部 curated 名单（无 count），仍可勾选保存。

## 六、默认值与迁移

- 升级后默认（未自定义过的用户）= "体育/电竞/天气不做、其余含未分类都做"，行为不变。
- 此前**自定义过** `excluded_categories` 的用户：旧覆盖行留在 `template_settings` 但不再被读取（无害死行），模板回落到新默认白名单。**发版说明须点一句**「品类筛选改为白名单勾选，请到配置页重新确认所选品类」。
- 不做数据迁移脚本（YAGNI）。

## 七、测试

- **纯函数**（`tests/test_categories.py` 改写）：`included_union`、`any_include_other`、`tag_pool` 打标签、`market_wanted`（命中/未命中/空 tags+include_other/空 tags 无 include_other 四种）。
- **scanner**（`tests/test_scanner.py` 改写）：mock `get_rewards_markets(tag_slug=)` 返回各 slug 的 cid 集，验证 `discover_candidates` 打标签正确、预筛按 included 并集 + include_other 生效；`filter_for_template` 白名单命中/未命中/其他兜底。
- **路由**：新增 `/api/categories` 计数测试（mock scanner API）；`test_settings_routes.py` 加 `included_categories`/`include_other` 持久化契约、去掉 `excluded_categories`；`test_database.py`（第 40、492 行）默认值更新为新键。
- **manager**：`_active_templates` 去重键含新维度（若现有测试断言旧键需更新）。

## 八、版本

过滤语义翻转（黑名单→白名单）+ 配置格式变更 → 按 `docs/版本号规范.md` 属 **MAJOR**：v2.4.0 → **v3.0.0**。

## 九、不做（YAGNI）

- 不让用户在 UI 里编辑 curated 名单本身（名单内置、由维护者增删）。
- 不做 Gamma event 标签打标签。
- 不做 `excluded_categories → included_categories` 自动迁移脚本。
- 不做每品类下的子筛选/正则/自定义 slug 输入。
