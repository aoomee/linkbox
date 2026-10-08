# LinkBox

简洁的 VLESS Reality / Shadowsocks 节点工具。只保留 **节点管理** 和 **进阶功能** 两个菜单。

```text
电脑 ── VLESS Reality ──→ 直连机 ── Shadowsocks ──→ 落地机 ──→ 网站
```

直连机提供 VLESS 服务，落地机提供 SS 服务。两台机器所在地区、商家和系统可以不同。

## 一键安装

两台机器都在 root SSH 终端执行同一条命令：

```sh
wget -qO- https://raw.githubusercontent.com/aoomee/linkbox/main/install.sh | sh
```

有 curl 也可以：

```sh
curl -fsSL https://raw.githubusercontent.com/aoomee/linkbox/main/install.sh | sh
```

以后直接输入：

```sh
lb
```

需要 Debian 11+、Ubuntu 20.04+（运行 systemd）或 Alpine（完整 OpenRC），Python 3.8+ 由安装器自动安装。支持 x86_64 / aarch64 / armv7l。普通没有 init 的容器不是安装目标。精简 Debian/Ubuntu 若没有 wget 或 curl，先运行 `apt-get update && apt-get install -y wget ca-certificates`。

## 两步连接

**1. 落地机：建 SS、复制 Token**

进入 `节点管理 → 创建 SS 落地`。填写名称、本机监听端口和可达的公网入口，脚本生成 SS 密码、分享链接和 `LB1.` 开头的 Token。

**2. 直连机：粘贴 Token、得到 VLESS**

进入 `进阶功能 → 导入落地 → 创建 VLESS`，粘贴 Token，填写直连机的公网入口与监听端口。脚本生成 VLESS Reality 链接，导入电脑或手机的兼容客户端即可。

默认 SS 为 AES-256-GCM，密码随机生成；VLESS 使用 Reality + Vision。Reality 会生成 UUID、密钥和 Short ID，并检查握手域名的 TLS 1.3 可达性，不需要自己申请证书。客户端必须支持 VLESS Reality/Vision。

## 菜单

```text
LinkBox

1  节点管理
   创建 SS 落地 / 创建 VLESS Reality
   查看链接与导出 Token / 修改 / 删除

2  进阶功能
   导入落地并创建 VLESS
   切换已有 VLESS 的落地
   TCP / UDP 端口转发
   服务与诊断 / 卸载
```

- 可管理多个节点，不限一个入口或一个落地。每个 VLESS 入口有独立的 SS 出站路由。
- 切换落地只改直连机的出站，原 VLESS 链接不变。
- 落地不可用时链路失败，**不会自动改走直连机出口**。
- 单独创建 VLESS 节点使用本机出口；想走落地时使用“导入落地”流程。
- 可导入本工具 Token 或无插件的标准 `ss://` 链接。支持 AES-128-GCM、AES-256-GCM、ChaCha20-IETF-Poly1305；不兼容其他脚本的 ENC/ENC2 Token。
- 转发功能固定转发目标端口，不转换协议；转发规则也可在节点列表修改或删除。

## 公网入口和 NAT

**本机监听端口**是程序在服务器内部监听的端口；**公网连接端口**是客户端或另一台机器实际连接的端口。

例如商家提供 `A IP:50001 → 落地机内部:8388`：

- 公网 IP / 域名填 **A IP**。
- 本机监听端口填 **8388**。
- 公网连接端口填 **50001**。

Token 自动包含 A IP 和 50001，与 A IP 所属地区无关。直连机有 NAT 时同样填写自己的映射入口。

SS 端口需要 TCP/UDP，VLESS Reality 入口需要 TCP；跨 VLESS 传递的 UDP 仍由入口 TCP 连接承载。脚本不修改防火墙、安全组或商家映射。创建成功只确认配置、服务和本地监听，客户端端到端连接还需实际验证。

公网地址是 IPv4/域名时默认监听 `0.0.0.0`，IPv6 字面量时默认监听 `::`。可以在“修改节点”中更改监听地址。IPv6 和双栈的实际可达性取决于系统及网络配置。

## Token 与密钥

Token 是带版本号的 SS 连接信息编码，**不是加密保险箱**，持有者可以取得 SS 密码并使用节点。它只包含落地的连接地址、端口、算法、密码和名称，不含 SSH 密码或 Reality 私钥。请只在自己的机器之间传递。

导入会严格检查允许的字段、协议、地址、端口和长度，不执行 Token 中的命令。连接信息只保存到服务器本地，不上传到 GitHub 或第三方服务。

在落地机轮换 SS 密码后，旧 Token 失效；需要在直连机通过“切换落地”导入新 Token。仅修改本地标签或公网地址不会撤销别人已经拿到的密码。

## 安装与恢复

- 固定使用 sing-box **1.14.2** 官方发布文件，区分 glibc / musl 和 CPU 架构，核验固定 SHA-256。
- 自有路径和服务名 `linkbox`，不接管已有 sing-box、GOST 或其他节点服务。
- 配置先通过 `sing-box check`，再应用；状态和配置按代存储，通过原子链接一起切换。
- 检测端口冲突，应用失败尝试恢复原配置、服务运行状态和开机自启状态。
- 如果操作被强制中断，下一次运行 `lb` 会读取事务记录并恢复。
- 同时只允许一个管理窗口修改配置。保留当前和前一代配置，文件默认仅 root 可读。
- 修改、删除或切换节点会短暂重启 LinkBox 服务，因此可能中断本工具管理的其他活动连接。

## 文件与服务

| 内容 | 路径 |
| --- | --- |
| 快捷命令 | `/usr/local/bin/lb` |
| 管理器与核心 | `/usr/local/lib/linkbox/` |
| 当前配置 | `/etc/linkbox/current/config.json` |
| 当前节点状态 | `/etc/linkbox/current/state.json` |
| 配置历史与恢复记录 | `/etc/linkbox/` |

请通过菜单修改节点，避免直接编辑自动生成的配置。

Debian / Ubuntu：`systemctl status linkbox`，日志 `journalctl -u linkbox -n 50 --no-pager`。

Alpine：`rc-service linkbox status`，日志 `tail -n 50 /var/log/linkbox.log`。

菜单“停止”同时关闭开机自启；添加或修改节点会重新启用。卸载需输入 `DELETE`，仅删除本工具的节点、密钥、服务与程序，保留系统依赖及历史服务日志。

## 验证

仓库包含 Token/路由校验、事务回滚测试，以及真实的本地 VLESS Reality → SS → TCP/UDP 集成测试，包括 SS 落地停止后的无直连回退检查。CI 覆盖 Debian、Ubuntu、Alpine 的 x86_64 官方核心运行和 Ubuntu 的真实 systemd 生命周期；以仓库 [Checks](https://github.com/aoomee/linkbox/actions) 结果为准。其他 CPU 架构和用户自己的 NAT/网络仍需实机验证。

本地运行：

```sh
python3 -m unittest discover -s tests -v
python3 tests/integration.py /path/to/sing-box
```

集成测试需要 Python/OpenSSL 支持 TLS 1.3，以及 `openssl` 命令。

## 参考

交互流程参考 [singbox-lite](https://github.com/0xdabiaoge/singbox-lite)。本工具聚焦 VLESS Reality 与 SS，不包含 Xray、多协议大菜单、Argo、自动测速或系统调优。

配置依据 [sing-box 官方文档](https://sing-box.sagernet.org/configuration/)。管理器代码采用 MIT 许可；下载的 sing-box 核心遵循其自身许可。
