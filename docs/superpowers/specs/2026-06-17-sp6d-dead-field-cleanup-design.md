# SP6d：死字段代码清理（dead-field cleanup）设计 / spec

> 日期：2026-06-17
> 状态：待用户评审
> SP6（模板管理 UI）的收尾子块，也是 v4 接入(SP1-SP6)的最后一块。父背景见记忆 [[v4-strategy-integration-roadmap]]。

## 零、背景与定位

v4 接入过程中退役了几处代码,但定义与测试还留着。SP6d 删掉它们,收口。**确认(已 grep `engine/web/models/api/utils`)**:三者均无 production 调用,只剩自身定义 + 各自测试。

| 死字段 | 退役于 | 现状 |
| --- | --- | --- |
| `tiers_k`(模板键) | SP2/SP6c | 引擎档数用 `len(tier_rules)`,模板键无人读;SP6c 已去掉 UI 输入 |
| `held_condition_ids` | SP5b | `place_orders` 改用 `held_side_info`;旧函数无人调 |
| `needs_replace` / `engine/strategy_check.py` | SP2 | Step3 改 cancel-only 后无人调,整模块死 |

**范围**:删这三处死代码 + 其测试。**不动** `laddering.py` 的 `tiers_k`——那是 `build_ladder` 的**函数形参**(= `len(tier_rules)`,活代码),与模板键同名不同物。

**不在 SP6d**:陈旧 worktree `.claude/worktrees/agent-abc14a0dc9e132681`(旧分支遗留、非本系列,greps 噪声)——单独 `git worktree remove` 即可,不进本 spec。

## 一、清理项

### 1.1 `tiers_k` 模板键
- `config.py` `TEMPLATE_DEFAULTS`:删 `"tiers_k": 6,` 一行。
- `tests/test_database.py` `test_template_defaults_has_multitier_keys`:删 `assert TEMPLATE_DEFAULTS["tiers_k"] == 6` 一行(该测试其余断言保留)。
- `tests/test_place_orders.py` `_make_worker` 的模板 stub:删 `"tiers_k": 6,` 一行(place_orders 用 `len(tier_rules)`,不受影响)。
- `tests/test_settings_routes.py` `test_get_settings_returns_v4_params`:从断言键元组里删 `"tiers_k",`(删键后 GET 不再返回它)。
- **保留** `engine/laddering.py` 全部 `tiers_k`(形参,活)。

### 1.2 `held_condition_ids`
- `engine/positions.py`:删 `held_condition_ids` 函数(保留 `held_side_info`)。
- `tests/test_positions.py`:把 import 从 `from engine.positions import held_condition_ids, held_side_info` 改为 `from engine.positions import held_side_info`;删 6 个 `held_condition_ids` 测试(`test_includes_condition_with_positive_size` / `test_excludes_zero_and_negative_size` / `test_excludes_missing_or_empty_condition_id` / `test_empty_list_empty_set` / `test_yes_and_no_of_same_condition_collapse_to_one` / `test_none_and_string_size_are_handled`);保留 4 个 `held_side_info` 测试。

### 1.3 `engine/strategy_check.py` + 其测试
- 删整个文件 `engine/strategy_check.py`(`needs_replace`)。
- 删整个文件 `tests/test_strategy_check.py`(4 个测试)。

## 二、安全性论证

- grep 确认 `held_condition_ids` / `needs_replace` / `strategy_check` / `template["tiers_k"]` 在 production 目录(engine/web/models/api/utils)**零调用**;唯一引用是各自定义 + tests。
- 删除后没有任何活代码路径断裂——所以删的测试都是「被删死代码的测试」,不是活代码覆盖丢失。
- `laddering.py::build_ladder(..., tiers_k)` 形参不动(`compute_market_ladders` 传 `len(tier_rules)`),多档挂单行为不变。

## 三、测试 / 验收

- `python -m pytest -q` 全绿。
- **计数核对**:418 − 4(删 `test_strategy_check.py` 整文件)− 6(删 `test_positions.py` 的 6 个 held_condition_ids 测试)= **408**。`test_database`/`test_place_orders`/`test_settings_routes` 改的是断言/stub 行,不改测试函数数。若实测非 408,逐项核对差异来源(防误删活测试)。
- 删除后再 grep:production 目录对三者**零匹配**(除 laddering 的 `tiers_k` 形参)。

## 四、范围之外

至此 v4 接入(SP1-SP6)全部完成。无后续子块。陈旧 worktree 清理(可选)与本 spec 无关。
