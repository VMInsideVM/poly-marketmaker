# release.ps1 — 一键发布:构建 -> 算 SHA-256 -> 打 tag -> gh release create。
# 用法:先改 version.py 的版本号,再运行:
#   powershell -ExecutionPolicy Bypass -File release.ps1
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

# 1) 检查 gh CLI 已安装且已登录
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
  throw "未找到 gh CLI。请先安装:winget install --id GitHub.cli  然后运行 gh auth login"
}
& gh auth status
if ($LASTEXITCODE -ne 0) { throw "gh 未登录。请运行: gh auth login" }

# 2) 读版本号
$ver = (& python -c "import version; print(version.__version__)").Trim()
if (-not $ver) { throw "无法从 version.py 读取版本号" }
$tag = "v$ver"
Write-Host "准备发布 $tag" -ForegroundColor Cyan

# 3) tag 不能已存在(避免覆盖已发布版本)
& git rev-parse $tag 2>$null
if ($LASTEXITCODE -eq 0) {
  throw "tag $tag 已存在。请先在 version.py 提升版本号再发布。"
}

# 4) 构建安装包(build_installer.ps1 会用 version.py 注入版本)
& "$root\build_installer.ps1"
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

$setup = "$root\installer\PolymarketMarketMaker_Setup.exe"
if (-not (Test-Path $setup)) { throw "未找到安装包: $setup" }

# 5) 算 SHA-256 写同名 .sha256(纯 hash,UTF-8 无 BOM)
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
$shaFile = "$setup.sha256"
[System.IO.File]::WriteAllText($shaFile, $hash)
Write-Host "SHA-256: $hash" -ForegroundColor DarkGray

# 6) 打 tag 并推送
& git tag $tag
& git push origin $tag
if ($LASTEXITCODE -ne 0) { throw "git push tag 失败" }

# 7) 创建 Release 并上传 setup.exe + .sha256(发布说明自动生成)
& gh release create $tag $setup $shaFile --title $tag --generate-notes
if ($LASTEXITCODE -ne 0) { throw "gh release create 失败" }

Write-Host ""
Write-Host "已发布 $tag,资源已上传到 GitHub Release。" -ForegroundColor Green
