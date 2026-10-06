# 配置并添加 GitHub SSH 密钥

## 任务

为实验室主机或机器人的实际使用用户准备 GitHub SSH 密钥，在 GitHub 账户中添加公钥，并验证该用户可以通过 SSH 访问仓库。每台设备、每个需要访问 GitHub 的用户分别执行，不将私钥复制到另一台设备作为默认做法。

工具创建或复用本地密钥、写入用户的 SSH 配置，并展示公钥；GitHub 网页上的公钥添加步骤需要用户完成。

## 通过 NetPilot 执行

### 1. 打开密钥向导

在需要访问 GitHub 的设备上，以实际使用 Git 的普通用户进入项目目录并运行：

```bash
./network_setup.sh github-ssh
```

也可在主菜单选择 **7. 应用代理 → 6. 配置当前用户 GitHub SSH 密钥**。直接入口无需 sudo 或 NetworkManager，需要 Python 和 OpenSSH 的 `ssh`、`ssh-keygen`。通过 sudo 启动菜单时仍操作原调用用户；先核对向导显示的用户名和目录。

### 2. 新建或复用密钥

首次配置按以下提示填写：

| 软件提示 | 选择或输入 |
|---|---|
| GitHub SSH 密钥 | **1. 生成新的 Ed25519 密钥** |
| 私钥文件名 | `id_ed25519_github`，可直接接受此默认值 |
| GitHub 邮箱或密钥备注 | 自己的邮箱或能识别本机的备注 |
| 确认为当前用户生成密钥 | 核对目录后填 `y` |

按 OpenSSH 提示输入本地私钥口令并再次确认。口令用于保护私钥，不是 GitHub 登录密码。私钥保存在 `~/.ssh/id_ed25519_github`，公钥为同名 `.pub` 文件。

已有这一对密钥时，选择 **2. 使用已有密钥** 并填写私钥文件名，工具会检查配对；同名文件已存在时不会覆盖。不要将私钥或口令贴到 GitHub、教程或仓库中。

### 3. 写入本地 SSH 配置

选择连接端口：

| 选择 | 连接目标 | 使用情况 |
|---|---|---|
| **1. 22** | `github.com:22` | 当前网络允许普通 SSH 连接 |
| **2. 443** | `ssh.github.com:443` | 22 端口受限时尝试 |

对“写入 `~/.ssh/config`”填 `y`。软件先备份已有配置，再写入所选密钥、用户 `git` 和端口。选择 443 后，仓库 SSH 地址仍可写为 `git@github.com:用户名/仓库.git`。443 上承载的仍是 SSH，第三篇教程的 HTTP／SOCKS5 应用代理开关不会自动转发它；在线验证需要独立可用的 SSH 网络路径。[GitHub 官方端口说明](https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port)。

### 4. 把公钥添加到 GitHub

向导会显示一整行公钥，通常以 `ssh-ed25519` 开头。复制这一整行，在浏览器中打开 [GitHub 添加 SSH 公钥页面](https://github.com/settings/ssh/new)：

1. 登录要访问实验室仓库的 GitHub 账户。
2. **Title** 填可区分设备和用户的名称，例如 `实验室机器人-客户端`。
3. **Key type** 选择 **Authentication Key**。
4. **Key** 粘贴向导展示的整行公钥。
5. 点击 **Add SSH key**，按网页提示完成身份确认。

添加的是公钥。网页保存后再返回向导进行认证测试。[GitHub 官方添加步骤](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account)。

### 5. 验证 GitHub 认证

回到向导，对“已将公钥添加到 GitHub，立即验证 SSH”填 `y`。首次连接时，先核对提示的主机指纹与 [GitHub 官方主机指纹](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)一致，再接受连接。

认证成功时输出中会包含 GitHub 用户名和成功认证信息；核对用户名确实是自己的账户。GitHub 不提供交互式 shell，成功的 `ssh -T` 仍可能返回退出码 `1`，工具会同时检查成功信息。[GitHub 官方验证说明](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/testing-your-ssh-connection)。

如果暂时没有可用 SSH 网络，先跳过在线验证，保留工具显示的后续验证命令。已经确认写入 SSH 配置时，也可之后运行：

```bash
ssh -T git@github.com
```

## 完成结果

本地用户拥有 SSH 密钥和 GitHub 配置，GitHub 账户已保存对应公钥。项目仓库需使用 `git@github.com:用户名/仓库.git` 这类 SSH 地址，并且账户拥有该仓库权限；HTTPS 地址使用另一套认证方式。

如果每次使用加密私钥都要输入口令，可按[GitHub SSH 密钥教程](../10_GitHub_SSH密钥.md)使用 SSH agent 缓存口令。NetPilot 不会自动在每个终端启动 agent。
