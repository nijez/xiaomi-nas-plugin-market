# 构建与验证

源码中保留设备管家的前端发布构建，便于无 Node 环境的安装端使用。修改该插件前端后先进入其目录运行 `npm ci`、`npm run check:runtime` 和 `npm run build`。不能只修改源码而不更新随包前端。

## rclone

Git 不保存 75 MiB 的可执行文件，Release 的 WebDAV 安装包包含它。自行构建时，从 rclone 官方下载 `v1.75.0/rclone-v1.75.0-linux-arm64.zip`，使用官方校验清单核对。

- 下载：https://downloads.rclone.org/v1.75.0/rclone-v1.75.0-linux-arm64.zip
- 官方校验：https://downloads.rclone.org/v1.75.0/SHA256SUMS
- 本版本锁定的压缩包 SHA-256：`d0ad88ba4c8e285b7c9efa591e0ab643280a91741e13c27f3a9c0957ccfa5203`

核对后将解压出的 `rclone` 放在 `projects/xiaomi-webdav-plugin/bin/rclone`。不要将 Mac/Windows 可执行文件放入 NAS 发布包；本机协议测试另通过 `RCLONE_BINARY` 指定本机版本。

## 签名构建

需要 Python 3.9+、Node.js、OpenSSL。部署目标当前为 RP05/Linux ARM64。

发布私钥必须保存在仓库之外，首次创建自己的 P-256 签名密钥：

```bash
umask 077
openssl ecparam -name prime256v1 -genkey -noout -out "$HOME/plugin-market-signing.pem"
python3 scripts/build.py --signing-key "$HOME/plugin-market-signing.pem"
python3 scripts/verify_release.py
```

输出在 `artifacts/`，包含跨平台安装 ZIP、5 个签名插件包、公钥及 SHA256SUMS.txt。默认保留已有输出，避免混入旧包；也可通过 `--output artifacts/v0.1.2-beta.1` 选择新的构建目录，再用 `verify_release.py --artifacts artifacts/v0.1.2-beta.1` 验证。

自己的签名密钥不是本仓库维护者密钥。自行构建相当于创建新的信任来源，必须向使用者单独说明公钥指纹；不要冒充原发布包。

## 验证

构建后进入各项目目录运行其 README 中的测试。最低要求：市场签名/解包/安装回滚测试、四个插件的 Python 回归、设备管家运行时完整性检查、WebDAV 沙箱交互与桌面/窄屏回归。

`verify_release.py` 会扫描源码与嵌套 ZIP 的敏感路径、私钥格式、常见令牌格式和禁止分发文件，并验证全部校验和、分离签名、包清单和 ZIP 完整性。仍需人工复核；测试账号示例不代表真实账户验收。

源码和 Release 均须先完成检查再提交/上传。此脚本不会自动 git push、创建 GitHub Release 或操作 NAS。
