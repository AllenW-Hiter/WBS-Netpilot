# NetPilot

Linux 网络设置、网络代理与 Git SSH 密钥辅助工具。网络设置基于 NetworkManager；代理和密钥入口可独立使用。

**开发版本：0.1.5.dev0。** 面向 Ubuntu 22.04/24.04 和 Debian 系系统；只有网卡配置功能要求 NetworkManager。真实网卡、无线驱动和端到端联网需在目标设备验收。

## 快速开始

保留完整项目目录，使用系统 Python；主菜单无需预先 sudo，需要写系统配置的具体操作才调用 sudo。AutoDL 的 root 可直接使用代理和密钥向导。安装缺失依赖前会询问，需要已有可用上行。

```bash
git clone --branch dev https://github.com/AllenW-Hiter/WBS-Netpilot.git
cd WBS-Netpilot
./network_setup.sh --check
./network_setup.sh --demo
./network_setup.sh
```

## 功能入口

| 主菜单 | 内容 |
|---|---|
| 1. 网络设置与检测 | 主机共享、从机/静态 IP、本机上行、连接管理、路由、备份恢复、网络诊断 |
| 2. 网络代理与检测 | 网线主机代理、SSH 代理、手动开关、可选 APT/Git 联动、当前代理检测 |
| 3. Git SSH 密钥辅助 | GitHub SSH 密钥生成/复用、配置与认证辅助、公钥文件查看 |
| 4. AutoDL 自动配置 | 多台服务器独立管理，粘贴 SSH 指令、隐藏输入密码，自动建立隧道、部署远端手动开关与验证 HTTPS |

代理分别使用 `wire_proxy_on/off` 和 `ssh_proxy_on/off`，新终端默认关闭。两种来源都影响当前终端和后来启动的应用；是否联动系统 APT、当前用户 Git 全局配置，在保存来源时分别选择。

AutoDL 可直接运行 `./network_setup.sh autodl-setup`，或传入登录指令：

```bash
./network_setup.sh autodl-setup --ssh 'ssh -p 2222 root@server.example' --local-port 10818
```

密码随后隐藏输入，不接受明文密码参数；自动化可通过 `--password-stdin` 从标准输入提供。可重复添加不同服务器，共用本机代理端口并同时保持隧道。`autodl-status` 查看全部服务器及编号，`autodl-start --server 编号` 重连指定服务器，`autodl-stop --server 编号` 恢复指定服务器的本工具联动并停止其连接；只有一台时可省略编号，菜单中可直接选择。APT/Git 默认不联动，详见[SSH 代理教程](docs/02_远程服务器SSH代理.md)。Git 密钥向导支持 AutoDL 的 root 用户，密钥保存在其自身的 `/root/.ssh`。

本机代理软件换端口后，使用 AutoDL 子菜单 **5. 修改本机代理端口**，或 `autodl-edit --server 编号 --local-port 新端口` 保存，再重连生效。修改支持断线状态，保留远端代理地址和 APT/Git 恢复记录；若隧道仍在运行，会关闭所选服务器的旧隧道。

## 使用教程

1. [主机与从机配置：通过主机共享上网](docs/01_主从机共享上网.md)
2. [远程服务器通过 SSH 借用本机代理出口](docs/02_远程服务器SSH代理.md)
3. [为远程服务器配置 Git SSH 密钥](docs/03_远程服务器Git密钥.md)

## 使用边界

- 共享使用 IPv4 NAT，出口跟随主机系统路由。尚未实现下游独立出口、透明 VPN、强制 VPN、IPv6 共享、单无线网卡同时上行和热点、DHCP 静态租约管理。
- 新连接先内存试运行，默认 180 秒内输入完整大写 `KEEP` 才保存；原生检查点不可用时停止。改 IP 可能中断 SSH，请保留本机或其他恢复通道。
- 不自动接管 unmanaged 接口，不修改 Netplan renderer，不自动调整 Docker/UFW/nftables 策略。
- `wire_proxy_on/off`、`ssh_proxy_on/off` 设置当前 Bash 的应用代理；APT/Git 持久规则只按所选选项联动。其他终端、已有进程、Git SSH、UDP/ICMP 和系统服务需分别处理。
- SSH 隧道与代理开关分别控制；新终端默认不设置代理。旧 `netpilot_proxy_on/off` 在迁移后是网线来源的兼容别名。
- 备份、代理认证和诊断导出属于运行数据；私有权限不等于加密，仓库不包含这些文件。

## 分支

`main` 保存经过验证的发布版本；`dev` 用于后续开发，通过检查和评审后合入 `main`。发布标签标识具体版本。切换到开发分支：

```bash
git fetch origin
git switch --track origin/dev
```

版本变化见 [CHANGELOG.md](CHANGELOG.md)。

`SHA256SUMS.txt` 为 `dev` 专用校验清单，不随 `main` 发布。
