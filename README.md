# NetPilot

基于 NetworkManager 的 Linux 网络配置工具，用于主机共享、从机接入、本机上行、临时路由和应用代理。

**开发版本：0.1.1.dev0。** 面向 Ubuntu 22.04/24.04 和使用 NetworkManager 的 Debian 系系统。离线测试通过；真实网卡、无线驱动和端到端联网需在目标设备验收。

## 快速开始

保留完整项目目录，使用系统 Python；需要写系统配置时，启动器会调用 sudo。安装缺失依赖前会询问，需要已有可用上行。

```bash
git clone --branch dev https://github.com/AllenW-Hiter/WBS-Netpilot.git
cd WBS-Netpilot
./network_setup.sh --check
./network_setup.sh --demo
./network_setup.sh
```

## 功能与教程

| 功能 | 使用教程 |
|---|---|
| 环境检查、启动、试运行与确认 | [开始使用](docs/01_开始使用.md) |
| 有线/Wi-Fi 共享、热点、直连管理 | [主机共享与本地管理](docs/02_主机共享与本地管理.md) |
| DHCP、固定地址、持久/按需网关 | [从机接入](docs/03_从机接入.md) |
| 本机有线与 Wi-Fi 上行 | [本机上行](docs/04_本机上行.md) |
| 按需添加、撤销临时默认路由 | [临时联网路由](docs/05_临时联网路由.md) |
| APT、Git HTTPS、curl 等应用代理 | [应用代理](docs/06_应用代理.md) |
| 状态查看、诊断、故障处理 | [诊断与故障处理](docs/07_诊断与故障处理.md) |
| 连接管理、备份恢复、JSON 计划 | [连接管理与备份恢复](docs/08_连接管理与备份恢复.md) |
| 离线验证、分支及发布流程 | [开发与发布](docs/09_开发与发布.md) |

## 使用边界

- 共享使用 IPv4 NAT，出口跟随主机系统路由。尚未实现下游独立出口、透明 VPN、强制 VPN、IPv6 共享、单无线网卡同时上行和热点、DHCP 静态租约管理。
- 新连接先内存试运行，默认 180 秒内输入完整大写 `KEEP` 才保存；原生检查点不可用时停止。改 IP 可能中断 SSH，请保留本机或其他恢复通道。
- 不自动接管 unmanaged 接口，不修改 Netplan renderer，不自动调整 Docker/UFW/nftables 策略。
- `netpilot_proxy_on/off` 控制系统 APT 和当前终端的应用代理；其他终端、Git SSH、UDP/ICMP 和系统服务需要分别处理。
- 备份、代理认证和诊断导出属于运行数据；私有权限不等于加密，仓库不包含这些文件。

## 分支

`main` 保存经过验证的发布版本；`dev` 用于后续开发，通过检查和评审后合入 `main`。发布标签标识具体版本。切换到开发分支：

```bash
git fetch origin
git switch --track origin/dev
```

版本变化见 [CHANGELOG.md](CHANGELOG.md)。
