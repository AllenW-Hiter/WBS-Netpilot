# NetPilot 维护约束

本项目修改真实网络。开发时先阅读 README、docs/09_开发与发布.md 和 docs/07_诊断与故障处理.md。

继续已有开发时，同时阅读 [handoff.md](handoff.md)，以当前检出的源码和提交记录为准。

## 分支文件

`AGENTS.md`、`CLAUDE.md`、`SHA256SUMS.txt` 和 `handoff.md` 仅保留在 `dev`。准备合入 `main` 时，在发布提交中移除这四个文件，并检查文档引用；不得让后续合并重新带入。同步 `main` 的公共变更回 `dev` 时，保留这四个开发专用文件并重算校验清单。

## 不可破坏的边界

- 测试不修改开发机网络，不运行真实 apt 安装、nmcli 写操作、iptables/nft 清空或 netplan apply。
- 子进程参数使用列表，不拼接 shell 执行用户输入；密码不进入 argv、日志和计划导出。
- 不将应用代理描述为全局 VPN；共享出口跟随系统路由，未实现下游独立出口。
- 新连接先内存试运行、autoconnect=false，原生检查点创建失败就停止；确认前不持久化。
- 独立 ip route 变更用独立 systemd 恢复；只操作有本工具归属记录的精确路由。
- 原用户连接不可静默删除；删除前验证活动状态、来源、备份和凭据完整性。
- 不自动接管 unmanaged/netplan/networkd 接口，不擅改全机 Wi-Fi 开关和防火墙策略。
- 用失败状态表达部分提交/部分恢复，不把异常吞掉后打印成功。
- 私有文件权限不等于加密，不提交运行时备份/代理认证/真实诊断报告到仓库。

## 开发验证

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -m compileall -q netpilot.py netpilot_lib tests
bash -n network_setup.sh
./network_setup.sh --check
./network_setup.sh --dry-run --plan examples/client_static.json
```

新增功能同时新增正常和错误路径测试。实际操作系统、网卡和无线驱动验收另做记录；不可把模拟测试声明为硬件兼容测试。更新功能表、测试数量和尚未实现列表，使文档与代码一致。
