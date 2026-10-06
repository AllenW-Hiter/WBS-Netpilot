# GitHub SSH 密钥

为启动工具的普通用户准备 GitHub SSH 身份。通过 sudo 启动主菜单时，仍写入原调用用户的 `~/.ssh/`。例如 `robot` 运行工具时，文件位于 `/home/robot/.ssh/`。

## 进入向导

在主菜单选择 **7. 应用代理 → 6. 配置当前用户 GitHub SSH 密钥**。也可直接运行：

```bash
./network_setup.sh github-ssh
```

直接入口无需 sudo 或 NetworkManager；需要系统 Python 和 `openssh-client` 中的 `ssh`、`ssh-keygen`。缺失时会说明依赖，按所用发行版安装后重试。

## 创建或选择密钥

1. 选择“生成新的 Ed25519 密钥”或“使用已有密钥”。
2. 填写 `~/.ssh/` 中的私钥文件名，默认 `id_ed25519_github`；只填写文件名，不能填写完整路径。
3. 新建时填写 GitHub 邮箱或备注，确认后由 `ssh-keygen` 提示输入口令及再次确认。口令用于保护本地私钥，不是 GitHub 登录密码。直接回车可不设口令。
4. 复用时须同时存在私钥和同名 `.pub` 公钥，例如 `id_ed25519_github` 和 `id_ed25519_github.pub`。工具验证配对；加密私钥由 OpenSSH 提示输入口令。

已有同名私钥或公钥会停止新建，另选文件名或复用即可。目录为 `0700`，新私钥为 `0600`；已有私钥如果其他用户可读，先在自己的终端执行 `chmod 600 ~/.ssh/文件名`。符号链接、归属错误或损坏的密钥文件会停止该步骤。

私钥只保存在用户目录；向导显示的是公钥和指纹。运行文件应保留在用户目录，仓库中不要提交私钥、口令或 SSH 配置备份。

## 配置 SSH

选择端口：

| 端口 | 连接目标 |
|---|---|
| 22 | `github.com`，默认 SSH 端口 |
| 443 | `ssh.github.com`，可在 22 端口受限时尝试 |

确认写入 `~/.ssh/config` 后，工具在文件开头添加带 NetPilot 标记的 `Host github.com` 配置，指定用户 `git`、所选密钥和端口。原文件备份为同目录的 `config.before-netpilot-时间-随机后缀`，其他内容保留。重复配置时替换本工具标记块；标记不完整或重复会停止。原配置中的其他 GitHub 密钥、Include 或代理规则仍需按实际 Git 操作检查。

配置 443 后，仓库地址仍可使用 `git@github.com:用户名/仓库.git`。443 上运行的是 SSH；HTTP 应用代理开关不会为它转发流量。[GitHub 官方端口说明](https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port)。

## 添加到 GitHub 并验证

向导显示一整行公钥。打开 [GitHub 添加 SSH 公钥](https://github.com/settings/ssh/new)，填写便于识别的标题，选择 **Authentication Key**，粘贴整行公钥并保存。GitHub 网页可能要求验证登录身份。[官方添加步骤](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account)。

完成网页添加后，回到向导确认立即验证。首次连接时，核对 [GitHub 官方主机指纹](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints) 后再接受主机。测试使用所选密钥和端口；认证失败、连接失败或超时均报告失败，本地已完成的文件保留。

GitHub 成功信息会显示认证用户名。成功的 `ssh -T` 命令也可能返回退出码 `1`；工具同时检查成功信息，避免把单独的退出码 `1` 当成功。[官方验证说明](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/testing-your-ssh-connection)。

如果跳过在线测试，向导显示后续验证命令。已经写入 SSH 配置时，也可在普通终端运行：

```bash
ssh -T git@github.com
git ls-remote git@github.com:用户名/仓库.git HEAD
```

私有仓库还需要该 GitHub 账户拥有仓库权限。SSH 密钥用于 SSH 地址；`https://github.com/...` 仓库地址仍采用 HTTPS 的认证方式。

需要在当前终端缓存密钥口令时，先使用已有的 SSH agent；没有 agent 时再启动，然后添加私钥：

```bash
eval "$(ssh-agent -s)"  # 仅在没有可用 agent 时运行
ssh-add ~/.ssh/id_ed25519_github
```

后续终端能否复用 agent 取决于登录会话；向导不在 `.bashrc` 自动启动 agent。[官方密钥与 agent 教程](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent)。
