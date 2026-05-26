# GitHub Release 自动更新 — 设计文档

日期:2026-05-26
状态:已确认,待写实现计划

## 目标

1. 每次发布的安装包(`PolymarketMarketMaker_Setup.exe`)都发布到 GitHub Release。
2. 程序每次启动时自动检测是否有新版本,并在浏览器里弹窗询问用户是否更新。
3. 用户选「是」后,程序自动从 GitHub 下载新安装包、校验、静默安装并重启。

终端用户是非技术小白:双击 exe → 控制台窗口 + 自动开浏览器(Flask `127.0.0.1:8000`)。所有面向用户的文案为简体中文。

## 约束与现状

- 仓库 `VMInsideVM/poly-marketmaker` 为**公开(public)**,Release 资源可无认证下载。
- 打包链:PyInstaller(onedir, `console=True`)→ Inno Setup → `installer/PolymarketMarketMaker_Setup.exe`。
- 安装为**按用户安装**(`PrivilegesRequired=lowest` + `DefaultDirName={autopf}`),装到 `%LOCALAPPDATA%\Programs\PolymarketMarketMaker`,无 UAC。
- 运行时数据(DB/日志)在 `%LOCALAPPDATA%\PolymarketMarketMaker`,与安装目录分离,升级不动数据。
- 版本号目前只硬编码在 `PolymarketMarketMaker.iss`(`1.0.7`),代码中无版本常量。
- 模板:`base.html` 被登录后的页面继承;`setup.html`、`login.html` 是独立整页。返回用户启动时第一眼是登录页。
- HTTP 一律用 Python 标准库 `urllib`,**不新增第三方依赖**(打包更省事);版本比较用小函数解析 semver 元组,不引入 `packaging`。

## 选定方案:静默运行现有 Inno 安装包(方案 A)

程序检测到新版后:下载 `PolymarketMarketMaker_Setup.exe` → 用 `/VERYSILENT` 静默启动它 → 程序自身优雅退出 → 安装包覆盖旧文件(Inno 的 Restart Manager 兜底关闭残留句柄)→ 安装完由安装包重新拉起程序。

理由:100% 复用已验证的打包链,新增代码最少;自动更新"添加/删除程序"的版本号与快捷方式;程序内用 urllib 下载 + 直接 `CreateProcess` 启动,正常**不会触发 SmartScreen**(SmartScreen 主要拦的是资源管理器双击带 MOTW 标记的文件)。

已否决:
- **方案 B(单独 updater 助手 exe)**:多一个要打包/维护的组件,且绕过 Inno,卸载注册表/版本号不更新。
- **方案 C(下载 onedir 压缩包换文件夹)**:同样卡在"不能覆盖运行中的 exe",绕过 Inno 注册表/快捷方式,文件锁处理更脆弱。

唯一关键点:Windows 不能覆盖正在运行的 exe —— 靠"先 spawn 安装包(detached),本进程立即优雅退出"释放文件锁 + `CloseApplications=yes` 兜底解决。

## 组件设计

### ① 版本号单一来源

- 新建 `version.py`:`__version__ = "1.0.7"`。**以后只改这一个地方。**
- `PolymarketMarketMaker.iss` 改用 `#ifndef` 守卫,使命令行 `/D` 定义可覆盖:
  ```
  #ifndef MyAppVersion
    #define MyAppVersion "0.0.0"
  #endif
  ```
- `build_installer.ps1` 读取版本(`python -c "import version; print(version.__version__)"`),通过 `ISCC /DMyAppVersion=<ver>` 注入。
- 运行时程序 `from version import __version__` 拿当前版本,用于比对并显示在 UI("当前版本 v1.0.7")。

### ② 检测更新(后端)

新建 `web/update.py` 模块封装更新逻辑;在 `web/routes.py` 注册端点。

`GET /api/update/check`(**无需登录**):
- `urllib` 请求 `https://api.github.com/repos/VMInsideVM/poly-marketmaker/releases/latest`,带 `User-Agent` 头,5 秒超时。
- 取 `tag_name`(去掉前缀 `v`),与 `__version__` 用 semver 元组比较。
- 在 `assets` 中找:名字以 `.exe` 结尾的资源 → `browser_download_url` + `size`;名字以 `.sha256` 结尾的资源 → 其下载链接(用于校验)。
- 返回 JSON:`{update_available: bool, current: "1.0.7", latest: "1.0.8", notes: "<发布说明>", size: <字节>}`。
- **任何网络/解析/超时错误一律返回 `{update_available: false}` 并记日志,绝不抛出、绝不阻塞启动。**

semver 比较:把 `"1.0.8"` 解析为 `(1, 0, 8)` 元组比大小;无法解析的标签视为"无更新"。

### ③ 弹窗(前端)

- 新建 `web/templates/_update_modal.html`:弹窗标记 + 一段 JS。被 `base.html`、`login.html`、`setup.html` 三处 `{% include %}`(避免重复)。
- 页面加载时 `fetch('/api/update/check')`。若 `update_available`,弹出模态框:
  > **发现新版本 v1.0.8**
  > <发布说明 notes>
  > 是否现在更新?   [ 是 ]  [ 否 ]
- 点「否」:关闭弹窗,本次会话不再提示;下次启动重新检测(符合"每次打开都检测")。不做持久化的"跳过此版本"。
- 点「是」:`POST /api/update/apply` → 轮询 `GET /api/update/status`,展示**下载进度条**(百分比来自 status 的 `percent`)。
  - 状态推进:`downloading` → `verifying` → `installing`。
  - 到 `installing` 显示:"正在安装并自动重启,请稍候,几秒后程序会自动重新打开。"此时服务端进程会退出,浏览器与服务端断开属正常现象。
  - 若 `error`:显示错误信息 + "稍后重试",程序继续正常运行。

### ④ 应用更新(后端)

`POST /api/update/apply` → 启动**后台线程**执行,立即返回 `{started: true}`(或在已有任务进行时返回当前状态):

1. **安全闸**:若引擎正在运行(`manager` 存在且有活跃 worker),拒绝,返回错误信息:"引擎正在运行,更新会中断做市并使持仓失去止损保护,请先停止引擎再更新。"(启动时弹窗发生在登录前、引擎通常未启动,但仍加这道闸,防止用户登录跑起引擎后再点更新。)
2. 流式下载 setup.exe 到 `%LOCALAPPDATA%\PolymarketMarketMaker\update\PolymarketMarketMaker_Setup_<ver>.exe`,边下边按已下载字节/总 `size` 更新 `percent`(state=`downloading`)。
3. **校验 SHA-256**(state=`verifying`):下载 `.sha256` 资源内容,与本地文件实算的 SHA-256 比对。不匹配 → 删除文件、state=`error`、中止,**绝不运行**。
4. state=`installing`:`subprocess.Popen([installer_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)`,使其在父进程退出后存活。
5. 本进程优雅退出(若有 `manager` 调 `stop_all`,关 DB,然后退出进程),释放 exe 文件锁,交由安装包覆盖并重启。

`GET /api/update/status` → `{state: idle|downloading|verifying|installing|error, percent: 0-100, message: "<可选>"}`。状态存于 `web/update.py` 的模块级单例(单进程单用户,简单可靠)。

并发保护:`apply` 若检测到已有任务在 `downloading/verifying/installing`,不重复启动。

### ⑤ 安装包重启程序(.iss 调整)

现有 `[Run]` 带 `skipifsilent`,静默安装时**不会**重启程序。新增一条静默专用项:
```ini
[Run]
; 交互式首装:勾选框"立即启动"(保留原样)
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动程序"; Flags: nowait postinstall skipifsilent
; 静默(自动更新)安装:装完自动拉起程序
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runasoriginaluser; Check: WizardSilent
```
`[Setup]` 增加 `CloseApplications=yes`,让 Restart Manager 在文件被占用时兜底关闭运行中的程序句柄,避免"文件占用"导致更新失败。

### ⑥ 发布脚本

新建 `release.ps1`:
1. `$ErrorActionPreference='Stop'`;开头检查 `gh auth status`,缺失/未登录则提示 `winget install --id GitHub.cli` 并 `gh auth login`,然后中止。
2. 读 `version.py` 版本号 → `$ver`。
3. 调 `build_installer.ps1` 构建(版本号注入 .iss)。
4. 算 `installer/PolymarketMarketMaker_Setup.exe` 的 SHA-256 → 写 `installer/PolymarketMarketMaker_Setup.exe.sha256`(纯 hash 文本,UTF-8)。
5. `git tag v$ver`(若已存在则报错提醒先 bump 版本)→ `git push origin v$ver`。
6. `gh release create v$ver installer\PolymarketMarketMaker_Setup.exe installer\PolymarketMarketMaker_Setup.exe.sha256 --title "v$ver" --generate-notes`。

发版流程:**改 `version.py` → 跑 `release.ps1`**,其余全自动。

### ⑦ SmartScreen

程序用 urllib 下载(不写入 MOTW 区域标识)、直接 `CreateProcess` 启动安装包,正常**不会**触发 SmartScreen 拦截(SmartScreen 主要针对资源管理器双击带 MOTW 标记的文件)。安装包本身未做代码签名,首次手动分发时仍可能有 SmartScreen 提示(沿用现有《使用说明.txt》的"仍要运行"指引);自动更新路径不受影响。

## 数据流

```
启动 → 浏览器开 (login/setup/dashboard)
  → JS fetch /api/update/check
      → 后端 urllib 拉 GitHub releases/latest → semver 比对
      → {update_available, latest, notes, size, (sha256 url)}
  → 有新版?弹窗 是/否
      否 → 关闭(下次启动再检测)
      是 → POST /api/update/apply (后台线程)
            → 安全闸(引擎运行?拒绝)
            → 下载 setup.exe (percent ↑, state=downloading)
            → SHA-256 校验 (state=verifying)  ── 失败 → state=error,中止
            → Popen 安装包 /VERYSILENT (detached, state=installing)
            → 本进程优雅退出
      JS 轮询 /api/update/status 显示进度
  → 安装包覆盖文件 → [Run] WizardSilent 重启程序 → 新版本启动
```

## 错误处理与安全

- **检测非阻塞**:5 秒超时,所有异常吞掉并记日志,失败即"无更新",绝不影响程序正常使用。
- **执行下载来的 exe 前必校验 SHA-256**,不匹配绝不运行。
- **安全闸**:引擎运行时拒绝更新(更新会中断做市、使持仓失去止损保护)。
- 安装包启动失败 → state=`error`,程序继续正常运行,用户可稍后重试。
- 「否」不持久化,下次启动重新提示(符合"每次打开都检测")。
- **登录前可更新**:检测与应用都不需要加密密钥,更新发生在登录前最安全(无引擎运行、内存无密钥)。

## 不做(YAGNI)

- 不做增量/差分更新(整包重装即可)。
- 不做"跳过此版本"持久化、不做静默后台自动更新(必须用户确认)。
- 不做代码签名(超出本次范围;SmartScreen 沿用现有说明)。
- 不引入 `requests`/`packaging` 等第三方依赖。
- 不用 GitHub Actions(本地一键脚本即可)。

## 受影响/新增文件

- 新增 `version.py` — 版本号单一来源。
- 新增 `web/update.py` — 更新检测/下载/校验/安装逻辑 + 状态单例。
- 新增 `web/templates/_update_modal.html` — 弹窗 + JS 片段。
- 改 `web/routes.py` — 注册 `/api/update/check`、`/api/update/apply`、`/api/update/status`。
- 改 `web/templates/base.html`、`login.html`、`setup.html` — include 弹窗片段。
- 改 `PolymarketMarketMaker.iss` — `#ifndef` 版本守卫、`[Run]` 静默重启项、`CloseApplications=yes`。
- 改 `build_installer.ps1` — 从 `version.py` 读版本并 `/D` 注入 ISCC。
- 新增 `release.ps1` — 构建 + 算 SHA-256 + 打 tag + `gh release create --generate-notes`。

## 测试

- 单元测试(`tests/`,纯逻辑无网络):semver 比较函数(相等/更高/更低/无法解析)、release JSON 解析(挑出 .exe 与 .sha256 资源、缺资源的容错)。
- 手动验证:本地起一个假的 `releases/latest` 响应或临时发一个测试 tag,走完"检测→弹窗→下载进度→校验→静默安装→重启"全链路(参考打包验证坑:用 `Start-Process -RedirectStandardError` 看 exe 自己的日志,别只看端口响应)。
