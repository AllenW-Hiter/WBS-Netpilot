# 远程服务器通过 SSH 借用本机代理出口

## 适用场景

本机开启 VPN 或代理后能够访问目标网站，AutoDL 等远程服务器可以通过 SSH 登录，但自身无法访问所需服务。使用 SSH **反向转发**，把本机已有的应用代理端口接到远端，再手动运行 `ssh_proxy_on`。

```mermaid
flowchart LR
    A["远端应用"] --> R["远端 127.0.0.1:17890"]
    R -->|SSH 反向转发| L["本机 127.0.0.1:7890"]
    L --> P["本机已有代理出口 / VPN"]
    P --> W["目标网站"]
```

此方案为支持代理的终端应用提供出口。它不在远端建立全局 VPN，Git SSH、ICMP/UDP、已有进程和系统服务不会自动使用这些代理变量。`-R` 的转发方向见 [OpenSSH 手册](https://man.openbsd.org/ssh#R)。

## AutoDL 自动配置

本机已有 NetPilot、OpenSSH 和可用的本地代理端口时，可使用主菜单 **4. AutoDL 自动配置 → 粘贴 SSH 指令自动配置**，或在本机项目目录运行：

本功能选择的是应用代理端口，不会切换系统 TUN。若同时运行 Clash 和 v2rayN，填写当前使用的那个端口；多个 TUN 同时开启仍可能影响 SSH 的实际路由，建议只启用需要使用的一个 TUN。

```bash
./network_setup.sh autodl-setup
```

按提示分别填写：

| 项目 | 示例 |
|---|---|
| 完整 SSH 登录指令 | `ssh -p 2222 root@server.example`，复制 AutoDL 控制台当前提供的指令 |
| 本机已有代理端口 | `10818`，以本机实际服务为准 |
| 远端回环端口 | `17890`，需要是未占用的端口 |
| 协议 | HTTP/混合端口选 `http`；SOCKS5 端口选 `socks5h` |
| APT/Git 联动 | 两项独立选择，默认否 |
| 安装缺失依赖 | 明确允许后，才会在远端 root 环境安装缺少的 Python/curl/Git |
| SSH 密码 | 单独隐藏输入，不附在登录指令后面，不保存 |

程序会建立专属后台 SSH 连接、部署运行文件，检查远端监听仅在回环地址，验证 HTTPS 后安装 `ssh_proxy_on/off`。远端无需预先下载项目，也不会覆盖已有 Git 工作目录。已有 Python 3.10+（包括 Conda）时直接使用；缺少依赖且没有选择允许安装时，会停止并说明原因。依赖安装使用本次 SSH 通道，不写入持久 APT 代理。

运行文件部署后，远端管理入口是 `~/.config/netpilot/autodl-cli`。例如执行 `~/.config/netpilot/autodl-cli git-keys` 可进入 Git 密钥菜单，AutoDL 的 root 用户也可使用。

首次连接会要求核对 SSH 主机指纹。仅解析 SSH 登录目标、`-p` 端口和 `-l` 用户参数，不执行粘贴指令中的远程命令或任意 `-o` 选项。

也可通过命令行提供非敏感参数，再隐藏输入密码：

```bash
./network_setup.sh autodl-setup --ssh 'ssh -p 2222 root@server.example' --local-port 10818
```

可选参数为 `--remote-port`、`--scheme`、`--apt`、`--git`、`--install-deps` 和 `--test-url`。非交互调用可用 `--password-stdin` 从标准输入读取一行密码，不提供 `--password` 明文参数。`--accept-new-host` 表示明确允许信任首次主机密钥，已改变的主机密钥仍会被拒绝。

配置完成后，在远端 Bash 手动开启：

```bash
source ~/.bashrc
ssh_proxy_on
# 在本终端启动需要代理的应用。
ssh_proxy_off
```

本机使用以下命令管理这组自动配置：

```bash
./network_setup.sh autodl-status
./network_setup.sh autodl-start
./network_setup.sh autodl-stop
```

状态显示本地 SSH 连接和最近一次 HTTPS 验证结果；重新连接需要再次输入密码。SSH 和转发仍在时，`autodl-start` 可直接重试上次未完成的部署。停止会先恢复本功能归属的远端联动，再关闭自己的 SSH 连接；已有远端终端仍需执行 `ssh_proxy_off` 恢复变量。配置或恢复部分失败时保留记录，不报告全部成功。连接已断开且无法确认 APT/Git 恢复时，需要先重连，或在远端手动关闭。

当前每个本地用户管理一组 AutoDL 配置，更换服务器前先停止原连接。若之前的手动隧道已占用 `17890`，先结束原隧道或选择其他端口；自动配置不会接管已有监听，也不会关闭其他 SSH 会话。

下文保留逐步手动配置方法。

## 1. 准备本机代理和远端项目

本篇以无认证的本机 HTTP 代理为例：

| 参数 | 示例值 | 在哪里使用 |
|---|---|---|
| 已有代理协议 | `http` | 两端配置一致 |
| 本机代理入口 | `127.0.0.1:7890` | 本机 VPN/代理软件提供 |
| 远端监听端口 | `17890` | 远端应用连接 |
| SSH 服务器 | `root@server.example` | 替换为实际用户和主机 |
| SSH 登录端口 | `2222` | 替换为平台提供的端口 |

`7890` 也可能是 `10808` 等端口，以本机代理软件配置为准。先在本机显式测试这个端口：

```bash
curl -q --proxy http://127.0.0.1:7890 --noproxy '' -I --max-time 15 https://example.com
```

将测试网址换成自己实际需要访问的 HTTPS 站点。只有 TUN/VPN 网卡、没有 HTTP 或 SOCKS 监听端口时，需先准备应用代理入口；本工具不会自动把 TUN 转换成应用代理。HTTP/混合入口通常选 `http`，已有 SOCKS5 入口则选 `socks5h`；`https` 表示代理入口自身使用 TLS。

远端需具备 Bash、`/usr/bin/python3` 和项目运行文件。若远端还无法在线克隆，可从本机项目根目录复制：

```bash
# 本机执行；先替换实际 SSH 用户、主机和端口。
ssh -p 2222 root@server.example 'mkdir -p ~/WBS-Netpilot'
scp -P 2222 network_setup.sh netpilot.py root@server.example:~/WBS-Netpilot/
scp -P 2222 -r netpilot_lib examples root@server.example:~/WBS-Netpilot/
```

本机使用 OpenSSH，远端 SSH 服务需要允许反向 TCP 转发。此代理功能不要求远端安装 NetworkManager。

## 2. 在本机配置反向隧道

Linux 本机的 NetPilot 项目目录中运行：

```bash
./network_setup.sh ssh-tunnel-config
```

等同主菜单 **2. 网络代理与检测 → 配置 SSH 隧道方向与端口**，填写：

| 向导项目 | 本例选择 |
|---|---|
| SSH 代理方向 | 远端借本机已有代理（AutoDL） |
| SSH 主机别名或用户@主机 | `root@server.example` |
| SSH 端口 | `2222`；使用已有 SSH 配置时可留空 |
| 已有代理协议 | `http` |
| 远端回环监听端口 | `17890` |
| 本机已有代理端口 | `7890` |

保存只保存隧道配置。然后在本机启动：

```bash
./network_setup.sh ssh-tunnel
```

首次连接核对远端主机指纹，按 OpenSSH 提示完成认证。隧道保持前台运行，正常时可能没有新输出。此终端要保持打开，本机也需保持联网和代理可用。

本机为 Windows/macOS，或只使用 OpenSSH 时，可直接在本机执行以下反向转发，替代本节的 NetPilot 隧道命令：

```bash
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:17890:127.0.0.1:7890 -p 2222 root@server.example
```

两种启动方式选一种，不要重复占用同一监听端口。这里两个 `127.0.0.1` 分别表示远端监听地址和本机代理地址，远端无需填写本机的局域网 IP。

## 3. 在远端配置 SSH 代理来源

另开终端登录远端，在远端项目目录执行：

```bash
cd ~/WBS-Netpilot
chmod u+x network_setup.sh
./network_setup.sh ssh-proxy
```

等同 **2. 网络代理与检测 → 配置 SSH 回环代理（ssh）**：

| 向导项目 | 本例填写 |
|---|---|
| 协议 | `http`，与本机代理服务一致 |
| 代理端口 | `17890`，与远端监听端口一致 |
| 认证 | 本机代理无认证则用户名留空；有认证按实际填写 |
| 绕过列表 | 通常保留默认值 |
| 联动系统 APT | 默认否；需要 APT 使用此入口时再选择是 |
| 联动 Git 全局 HTTPS 代理 | 默认否；需要持久 Git HTTPS 规则时再选择是 |

SSH 来源自动使用当前机器的 `127.0.0.1`。确认保存后，程序安装手动开关，不会立即开启代理或修改 APT/Git。

来源配置和脚本保存在 `~/.config/netpilot/`，权限为 `0600`，仍可能包含明文代理认证；私有权限不等于加密。配置、备份和恢复记录应保留在本地。

APT 联动会修改系统配置，普通用户开启时需要 sudo；Git 联动影响当前用户的全局配置。两者都可能影响其他终端。原有代理规则可能阻止联动，先通过状态检测确认来源，再按提示处理。

## 4. 在远端手动开启并验证

在远端 Bash 终端中执行：

```bash
source ~/.bashrc
ssh_proxy_on
./network_setup.sh proxy-status
./network_setup.sh ssh-proxy-test
curl -q -I --max-time 15 https://example.com
```

`ssh-proxy-test` 会询问测试网址，并显式使用保存的代理端点。`proxy-status` 只读检测当前进程继承的代理变量和可读的 APT/Git 规则，输出脱敏端点；APT 不可读时会提示需要权限。

启用后，从这个终端启动需要联网的应用，例如已经另行安装的 `codex`、curl 或 Git HTTPS 操作。已有应用需退出再启动，后台服务需单独处理。

同时在远端检查监听：

```bash
ss -ltn 'sport = :17890'
```

预期监听为 `127.0.0.1:17890`。如果发现 `0.0.0.0` 或 `::` 等通配监听，先停止隧道并请管理员核对 `GatewayPorts` 策略；不需要为了本方案开放公网 `17890`。服务端可以强制改变绑定范围，详见 [sshd_config](https://man.openbsd.org/sshd_config#GatewayPorts)。

验证应分别确认：本机代理可访问目标、SSH 隧道持续运行、远端监听存在、HTTPS 传输完成、实际应用请求成功。HTTP 401/403 可以说明请求收到了响应，但不代表账号授权或目标服务可用。

## 5. 日常开启与关闭

每次使用时，先在本机启动隧道，再到远端需要代理的 Bash 终端执行：

```bash
ssh_proxy_on
# 在此终端运行需要代理的应用。
ssh_proxy_off
```

首次加载函数或新开 Bash 时按需执行 `source ~/.bashrc`；加载函数不会自动启用代理。已有 `wire` 来源启用时，先执行 `wire_proxy_off`，再开启 SSH 来源。

旧版 `netpilot_proxy_on/off` 在迁移后是 `wire_proxy_on/off` 的兼容别名。

结束使用时先在远端执行 `ssh_proxy_off`，再在本机隧道终端按 Ctrl+C。关闭会恢复启用前的终端变量及所选 APT/Git 状态；停止隧道与关闭代理开关分别控制。

APT/Git 的恢复遇到外部编辑冲突时，开关返回失败并保留恢复记录。按提示修复冲突后重试 `ssh_proxy_off`，不要直接删除恢复记录。

## 同一局域网内使用网线主机代理

若从机能直接访问主机的应用代理端口，例如主机 `192.168.50.20:10808`，可以使用网线来源：

```bash
# 从机执行，填写实际主机 IP、端口和协议，分别选择 APT/Git。
./network_setup.sh wire-proxy
source ~/.bashrc
wire_proxy_on
./network_setup.sh wire-proxy-test
wire_proxy_off
```

主机代理需监听从机可达的局域网地址，并按现有访问策略限制客户端。仅监听主机的 `127.0.0.1` 不能供从机直接连接。网线来源的手动开关和 APT/Git 作用范围与 SSH 来源相同。

## 常见问题

| 现象 | 检查方向 |
|---|---|
| 本机已有代理端口不可达 | 核对 VPN/代理软件是否启动、实际端口和协议 |
| remote port forwarding failed | 检查远端端口占用及 SSH 转发权限，由管理员核对 `AllowTcpForwarding`、`PermitListen` 等限制 |
| 自动配置停在 HTTPS 校验，curl=35/28 | 先核对本机代理、所选协议及双 TUN 路由；修复后可用 `autodl-start` 重试，不将 CONNECT 200 当作 HTTPS 已成功 |
| 远端 connection refused | 检查本机隧道是否仍运行、远端监听端口是否一致 |
| curl 可用，Git SSH 不可用 | Git SSH 不使用 HTTP 代理变量，见[Git 密钥教程](03_远程服务器Git密钥.md) |
| 找不到 ssh_proxy_on | 确认配置发生在远端当前用户下，并在 Bash 中加载 `~/.bashrc` |
| 新窗口或已启动应用没有使用代理 | 在相应终端手动开启，并从该终端重新启动应用 |

本篇未实际连接你的远端服务器，出口和应用可用性以现场验证为准。
