# NetPilot 开发交接

更新时间：2026-10-06。供删除旧目录、重新 clone `dev` 后继续开发使用。以新检出的源码、分支及提交记录为准。

## 目标与分支

仓库：`git@github.com:AllenW-Hiter/WBS-Netpilot.git`。

- `main`：正式版本 `0.1.1`，已包含 GitHub SSH 密钥向导；发布标签 `v0.1.1`。发布提交为 `e274e776c79859815ecfacde8ef649a5ce0f4557`。
- `dev`：以 `main` 为基线，版本 `0.1.2.dev0`；包含本交接文件，后续功能在此开发、验证，再合入 `main`。
- `AGENTS.md`、`CLAUDE.md`、`SHA256SUMS.txt` 和 `handoff.md` 为 `dev` 专用；`main` 不保留这些文件。发布准备时显式移除，文档引用开发约束和交接文件时使用 `dev` 分支链接。CI 的校验清单检查仅对 `dev` 推送和指向 `dev` 的拉取请求执行。
- 原 `v0.1.0` 标签保留。版本来源为 `netpilot_lib/__init__.py`，README、CHANGELOG 与 SHA256 清单随分支更新。

重新开始：

```bash
git clone --branch dev git@github.com:AllenW-Hiter/WBS-Netpilot.git
cd WBS-Netpilot
git status --short --branch
./network_setup.sh --version
```

远端提交状态以 `git ls-remote origin refs/heads/main refs/heads/dev refs/tags/v0.1.1` 为准。旧会话普通推送遇到工具网络沙箱的 DNS 限制，沙箱外推送审批被策略拒绝；这些是旧工具环境限制。若本文件来自正常 GitHub clone，则该 clone 已取得包含交接文件的开发提交。

## 先阅读

[AGENTS.md](AGENTS.md)、[README.md](README.md)、[开发与发布](docs/09_开发与发布.md)、[诊断与故障处理](docs/07_诊断与故障处理.md)。使用教程在 `docs/`，按功能分成 10 篇；模块责任表见开发教程。

## 已确认的用户偏好和已完成行为

- 项目描述统一为通用主机/从机场景，README 保持简洁，设备型号专属称谓和旧示例命名已移除。旧设计文档及原始测试日志已替换为功能教程。
- 主机菜单第 2 项文案为“Wi-Fi 上行 → 有线共享（控制robot常用）”。
- 主从网段和地址由用户手动输入：接受 `192.168.1.0/24` 或 `192.168.1`；地址接受 `20`、`.20` 或完整 IPv4。取消预设菜单。主从地址必须不同、处于网段内，并检查接口/路由冲突。
- 新连接名称必须让用户输入；不要恢复为固定 `NP-HOST-*` 自动命名。
- `shared-dhcp-range` 支持无法确认时，明确选择全部 DHCP，或专用单台静态从机且不混接 DHCP。自动地址池不能保证为固定从机地址保留位置。
- 系统 `.tar.gz` 备份和单连接私有 JSON 备份是不同类型；恢复菜单读取后者。系统备份不是该恢复菜单的输入。
- 连接批量删除覆盖全部接口的活动 UUID 检查，包含虚拟接口及删除前再次检查。部分提交/回滚失败会返回失败，避免“看似成功”。

相关位置：`netpilot_lib/network.py`，`netpilot.py`；对应 `tests/test_address_wizards.py`、`test_connection_names.py`、`test_release_safety.py`。

## 应用代理

主菜单 **7**；也可运行 `./network_setup.sh proxy`。

- 用户确认保存后，备份并清理旧 APT / 当前用户全局 Git 代理项，安装 `~/.bashrc` 中的 `netpilot_proxy_on/off`。首次使用执行 `source ~/.bashrc`。
- `on` 先通过 sudo 写入并验证系统 APT 代理，再设置当前终端的代理环境变量；`off` 先撤销并验证 APT，再恢复该终端启用前的变量。失败会停止后续步骤。
- APT 状态影响系统 APT；Git HTTPS、curl 等采用当前终端环境。新终端不自动启用环境变量代理；Git SSH、ICMP/UDP、其他终端和系统服务需另行处理。
- 认证保存在 `~/.config/netpilot/` 私有文件，`.bashrc` 只定义加载函数。代理函数的 APT helper 引用当前项目路径；换目录后，使用代理菜单 **3** 重新安装/更新函数。
- 旧 `99proxy` 文件可在字典序上覆盖 `99-netpilot-proxy`。清理支持完整单行指令；嵌套或不明确的 APT 语法、符号链接会停止，保留备份供人工整理。
- Git 清理保留姓名和邮箱，只处理用户主目录内的全局/include 文件。仓库级、系统级和命令行代理需另外排查。

相关位置：`netpilot_lib/proxy.py`、`tests/test_proxy_switch.py`、[应用代理教程](docs/06_应用代理.md)。

## GitHub SSH 密钥功能

入口：主菜单 **7 → 6**，或 `./network_setup.sh github-ssh`。直接命令无需 sudo、NetworkManager；依赖 Python 和 `openssh-client`。

- 支持新建 Ed25519 或复用已有私钥及 `.pub`，默认文件名 `id_ed25519_github`；文件名可修改。
- sudo 菜单仍操作原调用用户，文件写入时降权，OpenSSH 子进程永久设置用户 UID/GID/组。直接 root 且无普通调用用户时停止。
- `ssh-keygen` 原生交互读取口令，不放入 argv、日志、配置或计划。生成先使用私有临时目录，再独占安装；同名文件、符号链接和错误权限会停止。
- 可选 `github.com:22` 或 `ssh.github.com:443`。确认后备份并写入 `~/.ssh/config` 的 NetPilot 标记块，保留其他配置内容。
- 向导展示公钥和 GitHub 添加页面，由用户在网页选择 Authentication Key 并保存；完成后可确认测试连接。首次连接须核对官方主机指纹。
- GitHub 成功认证常返回退出码 `1`；同时检查成功信息，支持 stdout/stderr 和换行，不将单独的退出码当成功。
- 密钥用于 Git SSH 地址。HTTP 代理开关不转发 SSH；未实现自动上传公钥或自动在 `.bashrc` 启动 agent。

相关位置：`netpilot_lib/github_ssh.py`、`netpilot_lib/proxy.py`、`netpilot.py`、`network_setup.sh`；34 项相关测试位于 `tests/test_github_ssh.py`。[使用教程](docs/10_GitHub_SSH密钥.md)。

## 开发边界与验证

遵循 AGENTS.md：测试使用模拟或临时用户目录，不改开发机网络、不运行真实 APT 安装、nmcli 写操作、清空防火墙或 netplan apply。用户输入用参数列表传给子进程；凭据不进 argv、日志、导出或仓库。

网络配置先原生检查点、内存试运行、`autoconnect=false`，完整大写 `KEEP` 确认后持久化；创建检查点失败就停止。独立临时路由由 systemd 恢复，只撤销本工具有归属记录的精确路由。原用户连接不能静默删除。

共享是 IPv4 NAT，出口跟随主机系统路由。应用代理和临时默认路由各自独立；避免承诺全局 VPN 或下游独立出口。不自动接管 unmanaged 接口、Netplan renderer、全局 Wi-Fi 或防火墙。

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -m compileall -q netpilot.py netpilot_lib tests
bash -n network_setup.sh
./network_setup.sh --check
./network_setup.sh --dry-run --plan examples/client_static.json
sha256sum -c SHA256SUMS.txt
```

最新功能代码通过 **227 项离线单元/模拟测试**，环境为 Ubuntu 22.04.5、系统 Python 3.10.12。SSH 测试生成临时加密密钥，通过临时终端读取口令，使用 `ssh -G` 检查配置；没有连接 GitHub、改真实用户 SSH 文件或改网。GitHub Actions 配置覆盖 Ubuntu 22.04/24.04，线上运行结果另行查看。

尚需实机验收：无线驱动、DHCP/NAT 下游联网、SSH 地址切换/检查点恢复、APT/Git 代理、GitHub 真实账户授权及在线 SSH。尚未实现：透明/强制 VPN、下游独立出口、IPv6 共享、单无线物理设备同时上行和热点、DHCP 静态租约管理。后续需求按用户最新指示实施。

`dev` 中受跟踪文件修改后重算 `SHA256SUMS.txt`，清单不包含自身，包含本文件；`main` 不生成清单。发布流程见开发教程，合入 `main` 前移除四个开发专用文件；同步公共变更回 `dev` 时保留它们。`.gitignore` 排除密钥、代理认证、运行时备份、实际诊断报告、编辑器配置和缓存。提交前检查暂存文件，遵循已有历史，不 force-push，不移动已有发布标签。
