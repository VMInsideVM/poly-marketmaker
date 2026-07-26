# Step3 挂单复查：并发预取 + 串行判定

日期：2026-07-26

## 问题

Step3（`engine/monitor.py:908` `check_sell_orders` → `:989` `_check_compliance`）是逐单串行的：`for o in open_orders:` 一单一单来，每单发两个网络请求——盘口（`:1026`，走 py-clob-client 的 httpx，超时按 httpx 默认 5 秒）和奖励（`:1075`，超时 10 秒）。前一单跑完才开始下一单。

钱包之间是并行的（每个启用钱包一个 `WalletWorker` 线程，`engine/manager.py:70`，各走自己的代理），所以瓶颈完全在单钱包内部。

用户实际规模是 5 个钱包、每个约 60 个在挂单。按代理往返 300ms 估算，单钱包一轮 Step3 要发 120 个串行请求、约 36 秒；代理慢到 500ms 就是 60 秒；代理抽风时最坏 60 × (5 + 10) = 900 秒。

代价落在止损上。`WalletWorker._run` 是「跑完一整轮 `_tick` 再等 5 秒」，而 Step3 排在 `_tick` 的最后一步：

```
check_buy_orders    成交检测
check_resolution
check_low_balance
check_exit          止损 / 止盈
check_sell_orders   Step3(最慢)
publish_status
```

所以 60 个挂单的钱包，成交检测和止损的真实间隔是几十秒到一分多钟，不是配置里的 5 秒。这个问题在加实时奖励复查之前就存在（那时每单 1 个请求），奖励复查让它大约翻倍。

## 目标

把单钱包内的 Step3 取数并发化，让一轮从 36 秒量级降到 10 秒以内，从而把止损间隔拉回同一量级。

不做的事：不改任何判定逻辑；不改钱包之间的并发模型；不引入配置项；不并发撤单（撤单只发生在少数单上，串行足够）；不动 `check_exit`、`check_buy_orders` 等其它 tick 步骤。

## 硬约束：worker 线程绝不能碰数据库

`models/database.py:17-30` 是每线程一条独立 sqlite 连接（2026-06-27 那次「共享连接读串了导致按错阈值强平」事故之后改的），连接建好后**永久留在 `self._connections` 列表里，从不回收**。

发现阶段每 4 小时新建一次线程池，泄漏可以忽略。Step3 每 5 秒一轮 × 5 钱包，如果 worker 线程访问 `db.conn`，一小时就是几千条不会关闭的 sqlite 连接。

这条约束决定了整个方案的形状：**线程池里只做纯网络读，DB 访问和所有副作用留在主线程**。代价是 `_market_rewards` 不能再自己去 `db.get_settings()` 读 TTL，得由主线程读好传进来。

## 方案

`check_sell_orders` 拆成三步。

### 第一步：主线程准备

读一次黑名单和 `rewards_cache_ttl_sec`（现在这两个是**每单读一次**：黑名单在 `:996`、TTL 在 `_market_rewards` 里，60 个挂单就是 60 次 DB 查询），然后挑出需要取数的买单：

- `side == "BUY"`
- `size_matched == 0`（部分成交的单现在就直接 `continue`，不进 `_check_compliance`）
- 市场不在黑名单（黑名单单直接撤，用不到盘口和奖励）

### 第二步：6 路并发取数

从这批单收集去重后的 `token_id` 与 `condition_id`，并发拿两张表：

```
books   = {token_id: orderbook 或 None}
rewards = {condition_id: (max_spread, daily_rate)}
```

去重是天然产物，比现在按市场记备忘更彻底：同一 token 上的多个挂单只取一次盘口，同一市场两侧只取一次奖励。

### 第三步：主线程串行判定

**遍历仍是全量 `open_orders`，不是只遍历预取的那批。** 现在这个循环除了买单复查还负责两类状态行：卖单记「止盈卖单 / 挂单中」，部分成交的买单记「Step1 / 部分成交」。只遍历预取子集会让监控状态表凭空少掉这些行。预取只是决定「哪些单的盘口和奖励要提前拿」，不改变遍历范围。

黑名单市场的单同理：不为它预取（它的分支在取数之前就 return 了），但它照常进 `_check_compliance` 被撤掉。

买单照现有逻辑逐单走 `_check_compliance`，盘口和奖励从上面两张表查，不再自己发请求。撤单、`record_action`、状态行全部留在主线程。

判定逻辑一行不改。`_round_market_rewards`（`:974-987`）与它的 `_round_rewards` 备忘（`:56-60`、`:910`）被预取取代，删除。

## `parallel_map` 上提到 `api/proxy.py`

`_parallel_map`（`engine/scanner.py:42`）已经解决了这件事上最危险的坑：`ThreadPoolExecutor` 的 worker 线程**不继承 contextvar**，`current_proxy` 会是 `None`，于是直连、泄漏真实 IP，违反「绝不直连」铁律。它捕获调用线程的 proxy，在每个 worker 里 set 回。

monitor 需要同样的能力。三条路里选第二条：

1. 从 scanner import 私有函数 —— monitor 依赖 scanner 的私有实现，方向不对。
2. **移到 `api/proxy.py` 改成公开的 `parallel_map(func, items, max_workers)`** —— 这个函数存在的唯一理由就是代理 contextvar 传播，放在代理模块里语义最正。
3. 在 monitor 里另写一份 —— 把防 IP 泄漏的关键代码复制成两份，最差。

`max_workers` 改成必传：两个调用方要的值不同（发现阶段 4、Step3 6），留默认值只会误导。scanner 的两个调用点（`:186`、`:348`）显式传 `_DISCOVERY_MAX_WORKERS`。`tests/test_scanner.py:727` 的 `TestParallelMap`（保序、空列表、代理传播、无代理保持 `None`）跟着实现挪到 `tests/test_proxy.py`。

Step3 的并发度写死为模块常量 `_STEP3_MAX_WORKERS = 6`，与 `_DISCOVERY_MAX_WORKERS = 4` 同一风格，不进配置页。6 把用户当前规模的一轮从 36 秒压到 6 秒；发现阶段之所以是 4，是因为实测「端点/代理娇气」，Step3 只打两个轻接口、不跑分页，略高一档可以接受。要回退只改这个常量。

## 取数失败的语义

现在 `get_orderbook`（`:1026`）**没有 try/except**，异常冒泡到 `check_sell_orders` 的逐单 `try/except`，结果是记一条 error 日志、该单本轮跳过、**不留状态行**。而盘口取到了但买卖盘为空，走的是另一条路：`:1124` 的「跳过(盘口为空)」分支，**会留状态行**。

预取必须保住这个区分：

- `books[token_id] is None` = 取数失败 → 该单跳过，等价于现在的异常路径（记 WARNING，无状态行）
- `books[token_id]` 是 dict 但 bids/asks 空 → 照走现有空盘口分支（留状态行）

这与 `daily_rate` 的 `0.0` 对 `None` 是同一种纪律：「拿到了一个空的」和「没拿到」不是一回事，塌成一个就会让状态行说谎。

奖励侧不需要新语义：预取失败填 `(None, None)`，现有判定已正确处理（`daily_rate is None` 跳过奖励闸门；`max_spread is None` 本轮跳过）。表里缺 key 按取不到处理，**不回退去现取**。

预取的每个任务自带 try/except、绝不外抛，失败就是 `None`。不额外重试：`api` 层已有 `_retry_on_connect_error`（只对连接建立阶段错误重试、绝不直连、读超时不重试以防重复下单），它在 worker 里照常生效。

## 还留在并发面上的东西

- `self._rewards_cache`（`:45-46`）会被 6 个 worker 并发读写。GIL 下 dict 的读写是原子的，不会崩也不会读到半个值，最坏情况是同一市场多取一次。默认 TTL 已是 0，这个缓存本来就不被读。
- `self.api` 会被并发调用。httpx 的 client 线程安全（`api/proxy.py:95` 每代理一个 `httpx.Client(http2=True)`），而且项目现在就已经在两个线程共用同一个 API 实例（采集线程下单 + 监控线程 tick），这次把并发度从 2 提到 6。
- 状态行、`record_action`、DB 读全部在主线程，不进并发面。

## 错误处理与不变量

- 判定逻辑与判定顺序一字不改。奖励闸门仍在空盘口守卫之前，`midpoint` 仍在其之后。
- 绝不 fail-close：取不到就跳过，绝不因为取数失败而撤单。
- 绝不直连：worker 必须带上调用线程的 `current_proxy`，这是 `parallel_map` 的职责，也是它上提到代理模块的理由。
- worker 线程不访问 `db`，不写状态行，不发撤单请求。
- 撤单仍然串行，仍然「撤不掉就 WARNING + return」。

## 测试

现有 800 个测试是主回归网。Step3 那几组（`TestStep3RewardDrop` / `TestStep3PriceBand` / `TestStep3EligibilityRecheck` / `TestStep3Blacklist` / `TestCheckSellOrders`）都是 mock `api.get_orderbook` 和 `api.get_rewards_for_market` 再驱动 `check_sell_orders`，预取后这些 mock 照样被调用，只是从 worker 线程调，断言原样成立。「同市场只取一次奖励」那几个测试也仍然成立——去重从备忘换成了预取，对外行为不变。

新增：

- 同一 token 上两个挂单，一轮只取一次盘口。
- 同一市场两侧各一个挂单，一轮只取一次奖励。
- 盘口取数抛异常 → 该单跳过、**不留状态行**、其余单照常处理。
- 盘口取到但 bids/asks 为空 → 仍走「跳过(盘口为空)」分支并**留状态行**。
- 黑名单市场的单不参与预取：断言没有为它的 token 取盘口。
- `parallel_map` 挪家后的 4 个原测试（保序、空列表、代理传播、无代理保持 `None`），移到 `tests/test_proxy.py`。

实施时要盯一件事：多线程下 mock 的调用**顺序**不再确定。要断言的都是判定阶段的产物（动作类型、状态行），那部分仍在主线程串行；但若发现旧断言隐含依赖调用顺序，改断言而不是改实现。

## 风险与回退

奖励端点就是发现阶段那个被实测认定「娇气」的端点。6 路 × 5 钱包 = 30 个并发打过去，虽然每个钱包只用自己的代理承 6 路、请求总数也远少于发现阶段整池，但这是这次改动最可能翻车的地方。回退很便宜：改 `_STEP3_MAX_WORKERS` 一个常量。

止损间隔不会回到 5 秒。预取约 6 秒，加上判定阶段的撤单（串行）与 `check_exit` 自身耗时，tick 周期预期从 40 秒量级降到 10 秒量级。

## 兼容性与发版

纯性能改动，无配置变更、无数据格式变更、无用户可见行为变更。按 `docs/版本号规范.md` 属于修订号；若与其它功能同批发版，跟随那一批的级别。
