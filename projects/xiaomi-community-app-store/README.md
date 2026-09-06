# 小米智能存储插件市场

0.1.3-beta.1 为公开测试安装包，非稳定版、非小米官方产品。包含当前加固代码、重新签名的五插件仓库和 115 ARM64 离线依赖。Windows 参数传输夹具通过不等于 Windows 10/11 实机安装验收；Mac/Android token/中继身份尚未完成验证，缺少可信证书身份时可能无法打开。115 和阿里云盘均需要使用者自己获批的 App ID。测试前备份，确保自己能通过 SSH 恢复；不要使用唯一副本数据测试。

面向已经通过授权流程开启 root SSH 的小米智能存储设备。用户只需部署一次商店，之后可在小米智能存储 Mac/Android 客户端内安装、更新和卸载社区插件。

## 安全边界

- 商店服务仅监听 `127.0.0.1:18119`，不直接暴露到局域网。
- 所有变更请求都要求商店自己的随机会话和 CSRF 令牌。
- 仓库清单和插件包使用 ECDSA P-256 分离签名，并额外校验 SHA-256。
- 插件包是声明式 `bundle-v1`，不允许携带或执行安装 shell。
- 解包时拒绝绝对路径、`..`、符号链接、设备文件和超限文件。
- 安装采用暂存、备份、`nginx -t`、服务健康检查、最后写注册表的顺序。
- 普通安装异常尝试回滚；停止候选服务失败时保留恢复材料并明确报错。断电恢复尚未实现。卸载默认保留插件数据，清除数据必须单独确认。

## 普通用户流程

以下为测试包安装流程。中继路径缺少可靠身份时会拒绝授权，请提交脱敏反馈，不要关闭鉴权。

1. 使用受信的一键脚本为自己的 NAS 开启密钥 SSH。
2. 下载商店发布包及 `SHA256SUMS.txt`，核对校验值。
3. Windows 用户双击 `install-windows.cmd`；Mac 可使用 `install-macos.command`；Mac/Linux 用户也可在终端运行 `bash install.sh`。安装器会询问 NAS 当前 IP、自动寻找常见位置的 root SSH 密钥，并尽量从小米客户端证书识别当前账号。
4. 重新打开小米智能存储客户端，在“基础应用”中打开“插件市场”。
5. 从小米客户端打开后会自动建立设备会话。后续安装和更新均在商店页面完成，不需要管理码，也不再需要输入 NAS IP。

Windows 小白用户可以直接阅读发布包内的 `WINDOWS-安装说明.txt`。分享时只需提供正式 ZIP 与对应的 `SHA256SUMS.txt`，不要附带任何个人 SSH 私钥、客户端证书或 NAS 会话令牌。

商店运行在 NAS 内部，因此 NAS 的 DHCP 地址变化不会影响插件页面。
商店使用每台 NAS 独立的内部密钥签发 30 天设备会话；密钥不会显示给用户，NAS 重启不会让会话失效。

应用入口使用 `community-store-v4.png`：蓝色下载符号铺满图标画布，四角为真实透明像素，针对手机客户端的小尺寸显示移除了旧版白色外圈。

安装器默认复用 root 脚本生成的 `~/.xiaomi-nas-root/nas-root-key`。密钥或用户注册表不在默认位置时，才需要设置 `NAS_SSH_KEY` 或 `NAS_USER_ID`。

无人值守安装也可以直接传参：

```bash
NAS_IP=192.168.31.100 NAS_SSH_KEY=/path/to/nas-root-key NAS_USER_ID=u123456 bash install.sh
```

Windows PowerShell 无人值守安装：

```powershell
.\install-windows.ps1 -NasIp 192.168.31.100 -NasSshKey C:\path\to\nas-root-key -NasUserId u123456
```

## 开发与验证

内置仓库包含设备管家、115 云备份、阿里云盘备份、WebDAV 文件桥和 qB 下载。WebDAV 文件桥自带经过官方 SHA-256 校验的 rclone ARM64 引擎，安装后共享默认关闭，远程连接需由用户填写自己的 WebDAV 账号。它不修改小米原有 5000 端口服务。

qB 下载为开发候选版，需要 Docker。首次由用户选择目录和密码后拉取锁定的 LinuxServer 镜像；尚未完成真实 NAS 下载或客户端验收。卸载插件仅停止它自己的容器，保留下载和配置。发行包不包含镜像本体。

```bash
python3 scripts/build_repository.py \
  --signing-key "$HOME/.local/share/xiaomi-community-store/signing-key.pem" \
  --create-key
python3 -m unittest discover -s tests -v
python3 server.py --dev
```

开发预览默认打开 `http://127.0.0.1:18119/`。`--dev` 只用于本机预览，不启用安装、更新或卸载动作。

## 发布原则

不要传播 `curl URL | sh`。正式发布应提供固定版本 ZIP、校验和、签名公钥指纹和可审阅源码。私钥只保存在离线发布机，不进入项目目录、ZIP 或 NAS。

自定义仓库不是“粘贴一个 URL 就信任”。用户必须同时导入该仓库公钥指纹；仓库换钥时需要再次人工确认。
