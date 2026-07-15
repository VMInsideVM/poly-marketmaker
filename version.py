"""version.py — 应用版本号的唯一来源。

发版只改这里;build_installer.ps1 读取它注入 Inno Setup,release.ps1 用它打 git tag,
运行时 web/update.py 用它与 GitHub 最新 Release 比对。
"""

__version__ = "6.0.2"
