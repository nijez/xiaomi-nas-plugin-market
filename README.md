# 插件市场

小米智能存储的第三方插件生态。**非小米官方产品，与小米、115、阿里云盘没有隶属或背书关系。**

在已开启密钥 SSH 的自有 NAS 上安装一次插件市场，之后从小米智能存储客户端安装和管理插件。页面随客户端连接当前设备，不要求 NAS 固定 IP。

## 下载与安装

在本仓库 **Releases** 下载 `xiaomi-plugin-market-0.1.2.zip` 和 `SHA256SUMS.txt`。不要用 GitHub 的 `Source code.zip` 代替安装包。

1. 确认 NAS 已 root，电脑与 NAS 在同一局域网，并且你持有这台 NAS 自己的 root SSH 私钥。
2. 核对 SHA-256 后完整解压，不要在压缩包内直接运行。
3. Windows 双击 `install-windows.cmd`；Mac 可运行 `install-macos.command`；Mac/Linux 也可在解压目录执行 `bash install.sh`。
4. 按提示填写当前 NAS IP，选择自己的 SSH 私钥和小米账号。安装器不会替未 root 的设备获取 root。
5. 退出并重新打开小米智能存储客户端，在全部应用中打开 **插件市场**。

Mac 如果拦截未签名的 `.command`，在终端运行 `bash install.sh`；不要关闭系统安全机制。Windows 需要安装系统可选功能 OpenSSH 客户端。

**每人使用自己的私钥，绝不共用发布者的私钥、证书或令牌。** 不需要另行输入管理码。

## 插件列表

| 插件 | 发布版本 | 状态与前置条件 |
| --- | --- | --- |
| [设备管家](projects/xiaomi-device-manager-prototype) | 0.4.1 | 只读查看 CPU、内存、温度、网络、磁盘和 Docker 状态 |
| [115 云备份](projects/xiaomi-115-sync-plugin) | 0.1.0 | 需要自己获批的 115 App ID，再扫码授权；不是免开发者认证登录 |
| [阿里云盘备份](projects/xiaomi-aliyundrive-sync-plugin) | 0.1.0 | 需要自己获批的阿里云盘应用，再扫码授权 |
| [WebDAV 文件桥](projects/xiaomi-webdav-plugin) | 0.2.0-rc5 | 测试版；多账号、多目录授权、远程单向备份、目录新建与原位改名 |
| [qB 下载](projects/xiaomi-qbittorrent-plugin) | 0.1.0-rc1 | 开发候选版；磁力/种子、进度、暂停继续、限速；首次初始化下载独立镜像，尚未完成真机下载验收 |
| [精速 ERP 入口示例](projects/xiaomi-erp-link-plugin) | 源码示例 | 自行配置网页上游地址和 NAS 用户；未纳入一键安装列表，不附 ERP 系统或业务数据 |
| 夸克网盘 | 未发布 | 目前没有可用实现，不提供空壳安装按钮 |

## 当前限制

- 首次公开分发为 **测试发布**，不是经过全机型验证的正式产品。已在自有 RP05 和 Mac 客户端开发验证；Windows 10/11 实体安装、其他固件和其他用户设备仍需测试。
- 第三方云盘接口、授权审批和限流由服务方决定。测试通过不代表已完成真实账户上传/下载验收。
- WebDAV 与云盘备份不是传播删除的双向镜像。使用前阅读各插件的覆盖、历史版本及冲突规则，重要文件另做备份。
- 不承诺官方 OTA 后入口与服务始终存在。WebDAV 会检查环境和目录身份；异常时拒绝共享，不自动放宽权限。
- 当前是随安装包分发的签名仓库快照；不会自动从 GitHub 拉取更新。更新市场请下载新安装包重新运行。自定义远程来源入口尚未开放。
- 不附带 root 获取工具、个人密钥、云盘令牌、NAS 配置、公司 ERP 或同步数据。
- qB 下载候选版未部署到维护者的 NAS；本地测试通过不等于 Mac/手机客户端、RP05 容器启动及真实下载已验收。

## 开发与贡献

各插件源码位于 `projects/`。目录名和内部 ID 保持兼容，用户可见名称统一为「插件市场」。

- [构建与验证](BUILD.md)
- [发布记录](CHANGELOG.md)
- [安全说明](SECURITY.md)
- [第三方组件与图标](THIRD_PARTY_NOTICES.md)
- [Windows 安装说明](projects/xiaomi-community-app-store/WINDOWS-安装说明.txt)

报告问题时附设备型号、固件、客户端版本和脱敏错误信息。不要上传账号密码、SSH 私钥、证书私钥或完整配置文件。
