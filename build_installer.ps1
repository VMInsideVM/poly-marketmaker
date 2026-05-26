# build_installer.ps1 — One-click rebuild: PyInstaller -> Inno Setup installer.
# Run from the project folder:  powershell -ExecutionPolicy Bypass -File build_installer.ps1
# Output: installer\PolymarketMarketMaker_Setup.exe

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

Write-Host "[1/2] PyInstaller: building dist\MarketMaker ..." -ForegroundColor Cyan
python -m PyInstaller MarketMaker.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

Write-Host "[2/2] Inno Setup: building installer ..." -ForegroundColor Cyan
$ver = (& python -c "import version; print(version.__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $ver) { throw "无法从 version.py 读取版本号" }
Write-Host "    版本号: $ver" -ForegroundColor DarkGray
$iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "ISCC.exe not found. Install Inno Setup 6 (winget install -e --id JRSoftware.InnoSetup)." }
& $iscc "/DMyAppVersion=$ver" "$root\PolymarketMarketMaker.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compile failed" }

Write-Host ""
Write-Host "Done. Installer: $root\installer\PolymarketMarketMaker_Setup.exe" -ForegroundColor Green
