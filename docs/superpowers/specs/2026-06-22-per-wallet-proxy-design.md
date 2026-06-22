# 每钱包 IP 代理（per-wallet proxy）设计 / spec

> 日期：2026-06-22
> 状态：待用户评审
> 背景：多钱包做市需让每个钱包的网络活动从各自的代理 IP 出口（IP 隔离，避免被平台按同 IP 关联多账号）。用户在钱包配置项里为每个钱包单独填代理（如 `gate.kookeey.info:1000:user:pass`），该钱包的所有网络活动都走它。

## 零、已确认决策

1. **协议**：仅 HTTP/HTTPS 代理（匹配 `host:port:user:pass` 格式，httpx 0.28 + requests 开箱即用，无新依赖）。
2. **失败语义**：代理不可用 → 该钱包本轮网络调用报错跳过，**绝不回退直连**（不泄露真实 IP）。天然实现：不 catch 直连重试。
3. **采集器**：共享市场采集用「采集所用钱包」的代理（零特例：凡经某钱包 API 实例发出的调用都走其代理）。
4. **凭证存储**：明文存 wallets 表（与 encrypted_key 同表，但 proxy 列明文）。
5. **每钱包可单独设置**；未配代理 = 直连（保持现行为）。

## 一、可行性约束（决定方案形状）

- `py-clob-client-v2` 的 HTTP 层是**模块级全局** `_http_client = httpx.Client(http2=True)`（`http_helpers/helpers.py`），`request()` 按模块全局名解析后 `_http_client.request(...)`；`ClobClient` 构造**不接受 proxy**，所有实例共享该全局 → 库**原生不支持 per-instance 代理**。
- 同一个 `PolymarketAPI` 实例被**采集线程**（`place_orders`）与**监控线程**（`_tick`）共用，下单还是在共享采集线程里为各钱包**顺序**执行 → 代理选择不能靠线程局部，必须按「当前操作的钱包」动态切换 → 用 `contextvars.ContextVar`。
- httpx 0.28.1 用 `proxy=` 参数（非 `proxies=`）；SOCKS 需 socksio（未装，本期不支持）。

唯一干净注入点：替换模块全局 `_http_client` 为「按 contextvar 选 per-proxy `httpx.Client`」的分发器（monkeypatch）。

## 二、架构

**传输层读 contextvar，操作边界设 contextvar。**

- **新模块 `api/proxy.py`**：contextvar + 解析 + httpx 分发器 monkeypatch + requests 包装。
- **`PolymarketAPI`**：携带 `proxy_url`；CLOB client 包一层 `_ProxiedClob` 使每次调用都在自身代理上下文内；9 处 `requests.get` 改走 contextvar-aware 的 `http_get`。
- **边界**（`_tick` / `place_orders` / 采集器扫描）：进入时 `use_proxy(该钱包.proxy_url)`，让其中的静态公共数据调用也走对的代理。
- **DB / 路由 / UI**：wallets 加 `proxy` 列（明文）；新增钱包表单与编辑入口；manager 构造 worker API 时传入。

## 三、`api/proxy.py`

```python
current_proxy: ContextVar[str | None] = ContextVar("current_proxy", default=None)

@contextmanager
def use_proxy(url):           # 设/复位 current_proxy
    token = current_proxy.set(url)
    try: yield
    finally: current_proxy.reset(token)

def parse_proxy(raw) -> str | None:
    """'host:port:user:pass' -> 'http://user:pass@host:port';'host:port' -> 'http://host:port';
    空/None -> None。已是 http(s):// 的原样返回。凭证 URL 编码;密码可能含 ':' 用 split(':',3)。"""

def install_clob_proxy():     # 幂等、一次性:替换 helpers._http_client 为分发器
def http_get(url, **kw):      # requests.get 包装,按 current_proxy 注入 proxies=
```

**分发器**：按 `current_proxy.get()` 选/缓存 `httpx.Client(http2=True, proxy=p)`（`p=None` → 直连 client，复用原全局以保持行为）；缓存 dict + 锁（多线程）。`request()` 调 `_http_client.request(**kw)`，分发器实现同名 `.request(**kw)`。

## 四、`PolymarketAPI` 改动

- `__init__(private_key, signature_type=2, funder=None, proxy=None)`：`self.proxy_url = parse_proxy(proxy)`；`install_clob_proxy()`（幂等）；`with use_proxy(self.proxy_url):` 包住 `temp_client.create_or_derive_api_key()` 等构造期网络调用；`self.client = _ProxiedClob(real_client, self.proxy_url)`。
- **`_ProxiedClob`**：`__getattr__` 返回的可调用属性包一层 `with use_proxy(self._proxy): return method(*a, **k)`；非可调用（如 `.builder`）原样返回 → 所有 CLOB 实例调用（含路由直接调用 cancel/balance）自动按钱包代理。
- 9 处 `requests.get(` → `http_get(`。`get_spread` / `get_user_positions`（实例、直用 requests）方法体 `with use_proxy(self.proxy_url):` 包住。
- 静态方法（rewards/gamma）的 `http_get` 读环境 contextvar（由边界设）。

## 五、边界与构造点

- `WalletWorker._tick`：`with use_proxy(self.api.proxy_url):` 包 4 个 monitor 调用。
- `WalletWorker.place_orders`：包方法体。
- 采集器扫描（用 `_scanner_api`）：包 `use_proxy(self._scanner_api.proxy_url)`。
- manager 构造每个 worker 的 `PolymarketAPI` 时传 `proxy=该钱包.proxy`；路由 `api_add_wallet` 构造时同样传入。

## 六、DB / 路由 / UI

- `models/database.py`：建表加 `proxy TEXT NOT NULL DEFAULT ''`；migration `ALTER TABLE wallets ADD COLUMN proxy ...`（沿用现有 col-exists 检测）；`add_wallet(.., proxy="")`；新增 `set_wallet_proxy(address, proxy)`；`list_wallets`/取钱包 SELECT 带 `proxy`。
- `web/routes.py`：`api_add_wallet` 读 `proxy` 字段并入库 + 传给构造；新增 `PUT /api/wallets/<address>/proxy`；钱包列表接口回 `proxy`（脱敏：仅回显 host:port + 是否有账密，或原样回显——见七）。
- UI（钱包页）：新增钱包表单加「代理（host:port:user:pass，可空）」输入；每行钱包加编辑代理入口。

## 七、待实现时定的小项（非阻塞）

- 钱包列表接口是否脱敏代理账密回显（默认：原样回显，单用户本地工具，与明文存储一致）。
- 代理改动是否需重建该钱包的 worker 才生效（默认：下次启动引擎生效，与「设置改动下次启动生效」一致；编辑后提示用户）。

## 八、测试

- `tests/test_proxy.py`：`parse_proxy` 边界（空/None、`host:port:user:pass`、`host:port`、密码含 `:`、已是 URL、凭证编码）；`http_get` 在 `use_proxy` 内注入 `proxies=`、外面不注入（patch `requests.get`）；分发器按 proxy 选不同 client（patch `httpx.Client`）。
- `tests/test_database.py`：`add_wallet`/`set_wallet_proxy`/列表读 proxy round-trip。
- `tests/test_*_routes.py`：POST 新钱包带 proxy 持久化；`PUT proxy` 更新。
- 既有钱包构造/调用测试：`PolymarketAPI(proxy=None)` 行为不变（直连），保证无回归。

## 九、范围之外

- SOCKS5（需 socksio 依赖）；代理健康检查/测速按钮；自动轮换/池化；对未配代理钱包的强制代理。
- 不改策略/离场/下单逻辑，仅在其网络出口加代理。
