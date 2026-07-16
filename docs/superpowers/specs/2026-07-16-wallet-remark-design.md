# 钱包地址备注（wallet remark）设计 / spec

> 日期：2026-07-16　　状态：已批准，待写实现计划

## 一、背景与目标

用户导入多个钱包后，界面里地址都是 `0x1234…abcd` 的截断形式，彼此难分辨（哪个是主号、哪个是小号）。
给每个导入的钱包加一个**可编辑的备注**（如「主号」「小号2」「朋友的」），并在**所有显示钱包地址的地方**
一并显示备注，让用户一眼认出是哪个号。备注纯展示，**不进任何交易/挂单决策**。

实现上把现有「代理（proxy）」那套字段的存/改/显示原样复刻一份即可（数据列 + `set_*` + `PUT` 路由 +
配置页 `prompt()` 编辑），再把备注挂到其余 4 个显示地址的前端页。

## 二、数据模型

`wallets` 表加一列：

```sql
remark TEXT NOT NULL DEFAULT ''
```

- 建表语句（`CREATE TABLE IF NOT EXISTS wallets`）加该列。
- 幂等迁移（与 `funder`/`proxy` 同款）：`PRAGMA table_info(wallets)` 查列，缺则
  `ALTER TABLE wallets ADD COLUMN remark TEXT NOT NULL DEFAULT ''` 后 commit。
- `list_wallets` 的 SELECT 带上 `remark`（于是自动流进 `/api/wallets` 响应）。

## 三、后端 API

沿用 `set_wallet_proxy` / `api_set_wallet_proxy` 的写法：

- `models/database.py` 新增 `set_wallet_remark(address, remark)`：`UPDATE wallets SET remark = ? WHERE address = ?`。
- `web/routes.py` 新增 `PUT /api/wallets/<address>/remark`（`@login_required`）：
  `remark = ((request.get_json() or {}).get("remark") or "").strip()[:40]`（空串=清空；**截断到 40 字**防撑破布局），
  `db.set_wallet_remark(address, remark)`，返回 `{"ok": True}`。**不**需要清 `_api_cache`（备注不影响任何 API 客户端）。
- 导入 `POST /api/wallets`（`api_add_wallet`）接一个可选 `remark`：
  `remark = (data.get("remark") or "").strip()[:40]`，透传给 `db.add_wallet(..., remark=remark)`。
  `add_wallet` 签名加末位 `remark: str = ""`，INSERT 带上该列。

## 四、配置页（编辑处，config.html）

- 钱包表头 `<tr><th>地址</th><th>存款地址</th><th>代理</th>…` 后加一列 `<th>备注</th>`（放在「代理」之后、「模板」之前）。
- 表体每行渲染备注格：`<td title="${esc(w.address)}">${esc(w.remark || '')}</td>`（空则显示空）。
- 操作列在「代理」按钮旁加「备注」按钮：`<button class="btn btn-sm" onclick="editRemark('${w.address}')">备注</button>`。
- 新增 JS `editRemark(address)`，完全仿 `editProxy`：从行数据缓存取当前备注 → `prompt('备注（留空=清除）：', cur)` →
  取消（`null`）则不动 → `fetch('/api/wallets/${address}/remark', {method:'PUT', body: JSON.stringify({remark: v.trim()})})` →
  成功 `showToast('备注已更新')` + `loadWallets()`。当前备注缓存：仿 `_walletProxies`，加 `_walletRemarks[address]=w.remark||''`。
- 导入表单加一个可选输入（放在 `new-proxy` 之后）：
  `<input type="text" id="new-remark" placeholder="备注（可选，如 主号/小号2）">`；
  `finalizeImport` 里 `const remark = document.getElementById('new-remark').value.trim(); if (remark) body.remark = remark;`；
  导入成功后清空 `new-remark`。

## 五、其余 4 处显示（约定 + 逐页）

**统一约定**（两个前端小工具，逐页内联，与现有 `shortWallet` 逐页重复的风格一致）：

```js
function shortAddr(a){ return a ? a.slice(0,6)+'...'+a.slice(-4) : ''; }
// 表格单元格/状态：有备注显示备注，否则短地址;完整地址永远进 title
function walletLabel(remark, addr){ return (remark && remark.trim()) ? remark : shortAddr(addr); }
```

- **dashboard.html**（钱包状态表，行来自 `/api/wallets`，有 `w.remark`）：
  地址格 `<td title="${esc(w.address)}">${esc(walletLabel(w.remark, w.address))}</td>`。
- **networth.html**（钱包下拉，选项来自 `/api/wallets`）：
  选项文字 = 有备注 `${esc(remark)} (${shortAddr(addr)})`、无则 `shortAddr(addr)`（下拉里带上短址便于按地址对齐）。
  用 `option.textContent`（安全，不必手动转义）。
- **history.html / logs.html**（都有：①钱包筛选下拉来自 `/api/wallets`；②表格「钱包」列渲染行里的**裸地址串** `a.wallet`/`r.wallet`）：
  - 下拉：同 networth，选项文字 = 有备注 `${remark} (${shortAddr})`、无则 `shortAddr`（`textContent`）。
  - 表格列：这两页 `fetch('/api/wallets')` 建下拉时**同时建一个 `地址→备注` 映射** `_remarkByAddr`，把现有
    `shortWallet(addr)` 升级为 `walletLabel(_remarkByAddr[addr] || '', addr)`（保持现有 `escapeHtml` 包裹 + `title` 放完整地址）。
- **markets.html** 只在内部用 `firstWallet`，无面向用户的地址展示，**不改**。

## 六、转义（必须）

备注是用户自由文本，渲染进 `innerHTML` 或 `title="…"` 前必须 HTML 转义，否则 `<`/引号会破坏 DOM/属性
（即便本地单用户、非恶意输入也会显示错乱）：

- history.html / logs.html 已有 `escapeHtml`，直接复用。
- config.html / dashboard.html / networth.html 目前只往模板串塞十六进制地址（安全）没有转义工具：给这几页加一个
  最小 `esc(s)`（`String(s).replace(/[&<>"']/…)`）并用于备注（及顺带用于放进 `title` 的地址）。
- 用 `textContent` 赋值的路径（下拉选项）天然安全，无需手动转义。

## 七、测试

- `tests/test_database.py`：`set_wallet_remark` 改值 + `list_wallets` 返回含 `remark`；新库迁移后 `wallets` 有 `remark` 列
  （旧库无 remark 列走 `ALTER TABLE` 后可读写——沿用现有迁移测试手法，若有）。`add_wallet(..., remark=...)` 落库。
- `tests/test_wallet_proxy_routes.py`（或同类路由测试文件）：`PUT /api/wallets/<addr>/remark` 更新成功、空串清空、
  超 40 字被截断；`POST /api/wallets` 带 `remark` 落库。
- 前端无单测（与现有一致）；交付后主会话实跑走查（导入带备注、编辑、5 处显示、含 `<`/引号的备注不破版）。

## 八、不做（YAGNI）

- 备注不参与任何筛选/排序/搜索、不进交易决策、不做富文本/多行。
- 不加独立的备注管理页；编辑只在配置页 `prompt()`（与代理一致）。
- 不改 `markets.html`（无面向用户的地址展示）。
- 不清 `_api_cache`（备注与 API 客户端无关）。
