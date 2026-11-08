# 为远程服务器配置 Git SSH 密钥

## 目标与准备

在远程服务器生成或复用 SSH 密钥，将公钥添加到 Git 托管平台，使服务器能够通过 SSH 执行 `git clone`、`git pull`、`git push`。本篇以 GitHub 为例，当前 NetPilot 内置向导也面向 GitHub。

以下终端操作均在远程服务器执行；添加公钥的网页可在本机浏览器操作。使用实际运行 Git 的用户登录服务器，并确认已经安装 `git` 和 `openssh-client`：

```bash
whoami
command -v git
command -v ssh
command -v ssh-keygen
```

密钥与 Linux 用户绑定。普通用户使用自己的 `~/.ssh`，root 使用 `/root/.ssh`。每台服务器可使用单独的密钥和备注，便于后续撤销。

## 1. 在远端运行 NetPilot 向导

在远端项目目录执行：

```bash
./network_setup.sh github-ssh
```

等同主菜单 **3. Git SSH 密钥辅助 → 配置 GitHub SSH 密钥并辅助授权**，也可运行 `./network_setup.sh git-keys` 进入该菜单。向导不要求 sudo 或 NetworkManager。

AutoDL 默认的 root 用户可直接使用同一向导，密钥保存在 `/root/.ssh`。普通用户使用自己的主目录；由普通用户通过 sudo 启动时，向导仍操作原调用用户的密钥。

| 向导项目 | 建议填写 |
|---|---|
| 密钥方式 | 没有专用密钥时选“生成新的 Ed25519 密钥” |
| 私钥文件名 | `id_ed25519_github`，只填文件名，不带 `.pub` 或路径 |
| 邮箱或备注 | 自己的邮箱，或 `autodl-github` 等可识别备注 |
| 密钥口令 | 按 OpenSSH 提示设置并确认；回车可不设置 |
| GitHub SSH 端口 | 先用 22；22 受限时可选 443 |
| 写入 SSH 配置 | 确认后写入当前用户的 `~/.ssh/config`，已有配置先备份 |

已有同名密钥时选择“使用已有密钥”，不要覆盖原私钥。复用时需同时存在私钥和对应 `.pub` 文件，向导会验证配对。

向导随后显示公钥并等待你添加到 GitHub。先完成下一节的网页操作，再回来确认“立即验证 SSH”。

## 2. 将远端公钥添加到 GitHub

只复制 `.pub` 公钥整行，例如以 `ssh-ed25519` 开头的那一行；私钥保留在服务器。

在本机浏览器登录自己的 GitHub 账号，进入[添加 SSH key 页面](https://github.com/settings/ssh/new)：

1. Title 填能识别服务器的名称，例如 `AutoDL-training`。
2. Key type 选择 **Authentication Key**。
3. 将远端生成的公钥整行粘贴到 Key，保存并按网页提示完成确认。

账户授权步骤见 [GitHub 添加公钥说明](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account)。向导显示公钥不会自动替你添加授权。

首次 SSH 连接时，按 [GitHub 官方主机指纹](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints) 核对后再接受。验证成功时会显示 GitHub 账号名称和认证成功提示；GitHub 不提供交互 shell，认证成功的 `ssh -T` 也可能返回退出码 1。

## 3. 需要手动配置时

普通用户和 root 均可直接使用向导。希望自行管理密钥和 SSH 配置时，也可使用下面的 OpenSSH 流程；所有操作在实际运行 Git 的远端用户下执行。

### 生成密钥

先查看是否已有要复用的密钥：

```bash
ls -la ~/.ssh
```

没有该目录属于正常情况。确认下面的文件名未被占用后执行；已有同名文件时复用原密钥或换名，不要同意覆盖：

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -C "autodl-github" -f ~/.ssh/id_ed25519_github
chmod 600 ~/.ssh/id_ed25519_github
cat ~/.ssh/id_ed25519_github.pub
```

按 OpenSSH 提示输入密钥口令。将最后显示的公钥按上一节添加到 GitHub。

### 选择密钥和端口

编辑 `~/.ssh/config`，为 `github.com` 添加或调整如下配置，保留已有的其他主机配置：

```sshconfig
Host github.com
    HostName github.com
    User git
    Port 22
    IdentityFile ~/.ssh/id_ed25519_github
    IdentitiesOnly yes
```

如果 22 端口无法连接，将同一个配置块的 `HostName` 改为 `ssh.github.com`、`Port` 改为 `443`，其余项不变。不要另追加一份相互冲突的 `Host github.com` 块。GitHub 的 443 端口仍运行 SSH 协议，说明见[官方端口教程](https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port)。

保存后执行：

```bash
chmod 600 ~/.ssh/config
ssh -T git@github.com
```

先核对主机指纹，再按提示输入密钥口令。若复用了旧配置，可用 `ssh -G github.com` 检查最终的 hostname、port 和 identityfile；OpenSSH 对多数配置项采用先取得的值，需核对已有通配规则的影响。

## 4. 使用 SSH 仓库地址

将 `YOUR_USER` 和 `YOUR_REPO` 换成自己的账号或组织、仓库名称。在远端克隆：

```bash
git clone git@github.com:YOUR_USER/YOUR_REPO.git
```

已有仓库需要切换为 SSH 地址时，在该仓库目录执行：

```bash
git remote set-url origin git@github.com:YOUR_USER/YOUR_REPO.git
git ls-remote origin
```

`git ls-remote` 是只读访问，可验证所选仓库是否可访问；账号 SSH 认证成功不代表对每个私有仓库都有权限。

需要设置提交署名时，再按实际身份填写：

```bash
git config --global user.name "你的姓名"
git config --global user.email "你的邮箱"
```

## 5. 远端网络受限时如何处理

[SSH 代理教程](02_远程服务器SSH代理.md)中的 `ssh_proxy_on` 和可选 Git 全局代理设置用于支持代理的应用及 Git HTTP/HTTPS 请求。Git SSH 需要自身能连接 GitHub 的 SSH 端口；改用 443 也不会自动让它经过 HTTP 代理。

先尝试向导中的 22/443 端口选项。如果两个 SSH 端口都不可达，可以在启用应用代理的终端使用 HTTPS 仓库地址：

```bash
git clone https://github.com/YOUR_USER/YOUR_REPO.git
```

私有库还需要相应的 HTTPS 凭据；SSH 公钥授权不代替 HTTPS 认证。需要继续使用 SSH 时，另行准备可达的 SSH 网络通道；本工具不自动配置 SSH 的 ProxyCommand。

## 验证与排查

| 现象 | 检查方向 |
|---|---|
| Permission denied (publickey) | 公钥是否添加到正确账号、当前 Linux 用户是否正确、所选私钥是否对应 |
| 连接超时 | 先检查网络和端口可达性；换密钥不能修复不通的网络 |
| 提示私钥权限过宽 | 确认私钥归当前用户所有，并设置 `chmod 600` |
| SSH 认证成功，仓库访问失败 | 核对仓库 SSH URL、账号权限和组织授权要求 |
| 旧版本提示只允许普通用户 | 更新到支持 root 向导的版本，或按本篇手动流程配置 |

预期结果是：远端保留私钥、平台保存公钥、SSH 识别到预期账号，并且 `git ls-remote` 能访问目标仓库。本文未实际生成你的密钥或验证账号授权。

重建容器前确认密钥所在目录是否持久化；停用实例时可在 GitHub 撤销对应公钥授权。GitLab 或自建 Git 服务可使用“远端生成密钥 → 平台添加公钥 → 按平台 SSH URL 访问”的流程，但其域名和端口以该平台提供的信息为准。
