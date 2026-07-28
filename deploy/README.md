# VPS 部署说明

把这个程序部署到一台 Linux VPS，通过域名 + HTTPS 远程使用。
设计背景见 `docs/superpowers/specs/2026-07-27-vps-deployment-design.md`。

## 前提

- 一台 Debian 12 或 Ubuntu 22.04+ 的 VPS，有 root
- 一个域名，A 记录已经指向这台 VPS 的 IPv4 地址（必须先解析生效，
  Caddy 申请证书时要用它验证域名归属）

## 部署

> **⚠️ 注意：`install.sh` 会用 `ufw --force enable` 重置防火墙规则，只放行
> 22/80/443 端口。这个脚本假设你用的是一台全新的、只跑这一个程序的 VPS——
> 如果这台机器上还跑着别的服务，先打开 `install.sh` 看一眼 ufw 那几行，
> 不然那些服务会被这次 enable 挡在外面。**

```bash
ssh root@<你的VPS地址>
git clone https://github.com/VMInsideVM/poly-marketmaker.git /tmp/pmm-src
bash /tmp/pmm-src/deploy/install.sh your-domain.com
```

脚本会：设时区为北京时间、建 `pmm` 服务用户、克隆代码到 `/opt/pmm/poly-marketmaker`、
建虚拟环境装依赖、安装并配置 Caddy、注册 systemd 服务、开启 ufw（只放行 22/80/443）。

完成后打开 `https://your-domain.com`，首次访问会进入设置页要求设定密码。

## 必做的 SSH 加固

私钥托管在这台机器上，SSH 弱口令等于把私钥送人。部署完立刻改
`/etc/ssh/sshd_config`：

```
PasswordAuthentication no
PermitRootLogin prohibit-password
```

然后 `systemctl restart sshd`。改之前先确认自己的公钥已经在
`~/.ssh/authorized_keys` 里，否则会把自己锁在外面。

## 日常运维

```bash
systemctl status pmm          # 看服务状态
journalctl -u pmm -f          # 看实时日志
systemctl restart pmm         # 重启(重启后需要重新登录网页)
```

**重启后必须有人登录网页。** 钱包私钥是用登录密码派生的密钥加密的，密钥只存在内存里，
进程一重启就没了。所以引擎不会自动恢复，得有人打开网页输密码、再手动启动引擎。
这是有意的设计——把密码存在服务器上就等于取消了加密。

## 更新

网页上有「更新」按钮，会 `git fetch` 到最新的 release tag、装依赖、然后退出进程，
由 systemd 用新代码拉起来。

- 引擎运行中点更新会被拒绝（更新要中断做市，持仓会失去止损保护），先停引擎。
- 任何一步失败都会自动回滚到原来的版本，进程继续跑，不会把服务搞挂。
- 万一某个新版本有启动期 bug 导致进程起不来，网页就打不开了，需要 SSH 上去手工回退：

```bash
cd /opt/pmm/poly-marketmaker
sudo -u pmm git reset --hard <上一个可用的 tag>
systemctl restart pmm
```

## 备份

`/opt/pmm/poly-marketmaker/market_maker.db` 存着加密后的私钥和全部历史数据。
它不在 git 里，更新不会动它。要备份就备份这个文件，但注意它只是密文——
没有登录密码解不开，所以密码得另外记牢。
