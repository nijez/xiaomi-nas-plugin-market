# 架构与后续路线

## 首版

一次性安装器通过用户已经配置好的 root SSH 上传商店。商店注册进小米客户端后，所有插件管理都发生在 NAS 本机：

```text
小米智能存储客户端
        |
        v
小米 Nginx /plugin/<user>/communitystore/
        |
        v
127.0.0.1:18119 社区商店
        |
        +-- 验证 catalog.json 分离签名
        +-- 验证 bundle ZIP 分离签名和 SHA-256
        +-- 安全解包到 staging
        +-- 声明式安装 / 健康检查 / 注册表提交
        +-- 失败回滚
```

商店不保存 SSH 私钥，也不需要知道 NAS 的局域网 IP。IP 只在首次从电脑上传商店时使用。

## 包格式

每个 `bundle-v1` 只允许这些顶层路径：

- `manifest.json`
- `runtime/`
- `ui/`
- `icon`
- `config/<service>.service`
- `config/<plugin>.conf`

包内没有安装脚本。目标目录、端口、插件 ID、健康检查和客户端注册信息都由 `manifest.json` 声明，并由商店再次验证。

## 自定义来源

下一阶段开放时必须同时满足：

1. 仅允许 HTTPS。
2. 用户添加 URL 时必须核对公钥指纹，URL 本身不代表可信。
3. DNS 解析结果不得落入本机、回环、链路本地或内网网段，防止 SSRF 和 DNS 重绑定。
4. 下载限制响应大小、超时和重定向次数。
5. 新来源只能安装声明式包，不能请求 shell、Docker socket 或任意文件写入。
6. 来源换钥时暂停更新，必须再次人工确认。

首版界面保留“添加签名源”位置，但按钮保持关闭，避免把尚未完成的安全边界包装成可用能力。

## 发布密钥

仓库公钥随商店发布，当前指纹记录在 `catalog/repository-public.sha256`。私钥默认位于发布者机器的：

```text
~/.local/share/xiaomi-community-store/signing-key.pem
```

私钥不进入 Git、发布 ZIP 或 NAS。若私钥泄漏，应发布新商店版本并明确吊销旧指纹，不能静默换钥。
