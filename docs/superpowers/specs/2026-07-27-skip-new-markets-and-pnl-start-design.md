# 跳过新建市场 + 台账起点前移 设计 / spec

> 日期：2026-07-27　　状态：已批准，待写实现计划

两件独立的小改动合并成一个 spec：新增「跳过最近 N 小时内创建的市场」开关，以及把每日盈亏台账的
补漏起点从 `2026-07-01` 前移到项目首次提交日。另附一条答疑记录（结算检查频率），无代码改动。

## 一、跳过新建市场

### 1.1 背景与目标

刚上架的市场盘口薄、价格未定型，奖励参数也可能随后调整，在上面做市风险高于成熟市场。
本功能让用户选择「市场创建满 N 小时之后才做」，N 可配。

**默认关闭。** 这是会改变下单行为的功能，默认开等于替用户改策略。参照
`low_balance_liquidation` 默认开那次，发版公告不得不专门交代。

### 1.2 数据来源：`created_at` 白拿

CLOB 奖励端点 `GET /rewards/markets/multi` 返回的每条市场记录自带 `created_at`
（2026-07-27 实测 300/300 全有值，格式 `2026-07-22T23:10:03.086269Z`，UTC 带 `Z`，小数秒位数不固定，
整串 24 到 27 字符）。Gamma `/markets` 的 `createdAt` 与之相差不到 1 秒，但要多发一次网络请求。

结论：零额外网络请求，纯本地判断，不拖慢首单。见 [[startup-to-first-order-timing]] 的教训，
发现阶段每多一次 per-market 网络往返都直接计入首单等待时间。

### 1.3 配置（模板级，`TEMPLATE_DEFAULTS`）

与 `min_settlement_days`、单价区间、`max_spread_cents` 等筛选门槛同级，每钱包按绑定模板取值：

- `skip_new_markets`（bool，默认 `False`）
- `new_market_hours`（float，默认 `24.0`）

`web/routes.py` 的 `/api/settings`、`/api/templates/<id>` 已按 `TEMPLATE_DEFAULTS` 白名单自动存取，
后端无需逐键改。**前端 checkbox 必须在 collect 函数里单独收**（`data.skip_new_markets = !!cb.checked`），
与 `config.html:506` 的 `include_other` 同一写法。白名单只管过滤，不会替你从 DOM 里读出 checkbox
和 select 的值，这是配置链路上踩过的坑（见 [[take-profit-position-driven]]）。

### 1.4 纯函数 `market_age_hours`（`engine/scanner.py`）

```python
def market_age_hours(created_at: str, now: float) -> float | None:
    """市场创建至今的小时数；created_at 缺失或解析不出返回 None（调用方 fail-open 保留）。"""
```

**不复用 `_parse_end_date`。** 那个函数是刻意按 naive 本地时区还原的：`end_date` 的语义是「日历日」，
去 `Z` 按本地解析再 `.timestamp()`，一去一回时区抵消，得到的正是字符串里写的那个日期。
`created_at` 是真正的 UTC 时刻，套同一套解析会平白差 8 个时区小时，把「25 小时前创建」算成「17 小时前」。

解析用正则取到秒、丢掉小数秒（最多差 1 秒，无意义），按 UTC 组装：

```python
_CREATED_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")
```

丢掉小数秒同时绕开了 `datetime.fromisoformat` 只认 3 位或 6 位微秒的限制（实测存在 2 位的样本）。
非 `Z` 结尾的时区偏移不做处理，一律按 UTC。实测样本 100% 带 `Z`，真出现别的格式时正则仍然匹配得上，
误差最大一个时区，不至于把判断掀翻。

### 1.5 判定落在两处

参数是模板级的，而发现阶段是钱包无关的共享阶段，所以判定必须做两遍。现有的奖励地板就是这个模式：
发现阶段用各模板最小的 `min_reward_usd` 兜底排除，`prefilter_for_template` 再按模板自己的值精筛。

| 位置 | 门槛 | 效果 |
|---|---|---|
| `discover_candidates` | 最宽松值：所有模板都开了开关才生效，取各模板 `new_market_hours` 的最小值；任一模板没开即为 0（不排除） | 新市场不进候选池，连订单簿都不抓；市场发现页也看不到 |
| `prefilter_for_template` | 该模板自己的 `new_market_hours` | 多模板时各自精筛 |

发现阶段的门槛由新纯函数给出：

```python
def loosest_new_market_hours(templates) -> float:
    """发现阶段可安全排除的门槛。任一模板没开该开关 -> 0（不排除任何市场）；
    全开 -> 各模板 N 的最小值。templates 为空 -> 0。"""
```

两处都 fail-open：`market_age_hours` 返回 `None`（`created_at` 缺失或畸形）时保留该市场，
与结算窗口解析不出即保留的口径一致。宁可多做一个市场，不可因为一个字段格式变动就整池不下单。

`new_market_hours = 0` 视同不筛，与开关关闭等效。两处判定都写成 `if hrs and age is not None and age < hrs`，
不用「是否配置了这个键」当条件。

### 1.6 必须同步扩 `_active_templates` 的去重键

`engine/manager.py:983` 的 `_active_templates` 按一个 key 给模板去重，注释写明「去重键须含采集器实际
用到的每个维度」。发现阶段现在要读 `skip_new_markets` 和 `new_market_hours`，这两个键就得加进 key：

```python
bool(tmpl.get("skip_new_markets", False)),
tmpl.get("new_market_hours"),
```

漏掉会出真 bug：模板 A 开了 24 小时、模板 B 没开，其余维度全同，去重后只留下一个。留到 A 就等于
把 B 的市场也一起剔了；留到 B 则 A 的开关白配。窗口和档位当初就是踩了这个坑才补进 key 的。

### 1.7 两个可见的副作用（已与用户确认接受）

**生效延迟最多 4 小时。** 发现轮间隔是 `discovery_interval_sec = 14400`（4 小时）。市场在发现那一刻
若是 23 小时龄会被排除，要等下一轮发现才可能进池，实际保护期落在 N 到 N+4 小时之间。
判定放在 `prefilter_for_template` 才能精确到 30 秒的下单轮，代价是市场发现页会照常显示这些市场。
用户选了发现阶段排除，接受这个偏保守方向的延迟。

**开启开关的那一刻，已挂在新市场上的买单会被撤。** 市场跌出候选池后，下一个下单轮的
`cancel_dropouts` 会撤光该市场买单（cancel-only，不设冷却，持仓不动）；该钱包正对这个市场
处于冷却中时是例外——dropout 判定带冷却豁免，那笔买单要等冷却过期才撤。把 24 调大到 72 时同理。
这符合「我不想在新市场做市」的意图。反向不必担心：时间只会前进，市场只会变老，不会重新变「新」。

## 二、台账起点前移

`engine/manager.py:30` 的 `PNL_START_DATE` 从 `"2026-07-01"` 改成 `"2026-05-17"`，即 git 首次提交日
（`27cc9bc docs: initial project spec and design documents`）。

改这一行就够，没有网络代价：`rebuild_wallet_pnl` 本来就拉全量 `/activity` 与 `get_trades`、不带
时间过滤，`_date_range(from_date, to_date)` 只决定往 `daily_pnl` 写哪些天。前移 45 天等于多写 45 行
本地 SQLite 记录，没有交易的日子写全 0 行（现有逻辑本就每天都写，含空日）。

连带变化：周报里「累计净利润（自 X）」的 X 跟着变（`engine/manager.py:909/921/938` 三处引用同一常量，
无需分别改）；`/pnl` 页面固定拉 365 天（`pnl.html:47`），改完自动显示全程。

起点写死为常量，不做成配置项。用户明确不需要自己调，多一个配置项就多一份校验和前端表单。

## 三、答疑记录：结算检查频率（无代码改动）

用户问「判定市场是否进入提交结算，检查频率是怎样的，对已挂单市场是否与挂单重测同频」。
结论是每 5 秒一轮，同一个 tick 里挨着跑。`WalletWorker._run` 每 `fill_check_interval_sec`（默认 5 秒）
调一次 `_tick`，一个 tick 的顺序是：

```
check_buy_orders → check_resolution → check_low_balance → check_exit → check_sell_orders → publish_status
```

结算守卫共三个点，频率不同：

| 检查点 | 位置 | 管什么 | 频率 |
|---|---|---|---|
| `check_resolution` | `monitor.py:277` | 有在挂**买单**的市场进入结算 → 撤光该市场全部买单 | 5 秒（与 Step3 重测同 tick） |
| `check_exit` 顶部 | `monitor.py:491` | 有**持仓**的市场进入结算 → 市价清仓（覆盖「永不低于成本」） | 5 秒（同一个 tick） |
| `place_orders` | `manager.py:280` | 下单前跳过在结算的市场 | 30 秒（下单轮） |

每次只对「当前有买单/有持仓」的 condition_id 批量查一次 Gamma `/markets?condition_ids=`，不是全池扫。
Gamma 抖动返回 `{}` 时 fail-open，一律不撤、下个 tick 重试。

## 四、测试

`tests/test_scanner.py`：

- `market_age_hours`：小数秒 0 位 / 2 位 / 6 位都能解析；`created_at` 缺失、空串、畸形串返回 `None`；
  UTC 语义正确（构造一个已知 UTC 时刻，断言算出的小时数不受本机时区影响）。
- `loosest_new_market_hours`：全开取最小值；一个模板没开返回 0；空列表返回 0。
- `discover_candidates`：全模板开启时新市场不进候选池；有模板没开时照常进；`created_at` 缺失的市场保留。
- `prefilter_for_template`：按模板自己的 N 筛；开关关闭时不筛；`created_at` 缺失时保留。

`tests/test_database.py`：两个新键在 `TEMPLATE_DEFAULTS` 里、默认值正确（该文件已有同类契约断言）。

`tests/test_manager.py`：`_active_templates` 对「只有 `skip_new_markets` / `new_market_hours` 不同」
的两个模板不去重（该文件已有窗口/档位维度的同类用例可仿）。

`tests/test_manager.py` 或 `test_pnl_ledger.py`：`PNL_START_DATE == "2026-05-17"` 契约断言。

## 五、不改的东西

结算守卫、离场（`check_exit` / `plan_exit`）、Step3 挂单重测、下单定价与档位逻辑一概不动。
第三节纯属答疑，落到文档为止。
