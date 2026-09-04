# 阿里云盘备份（小米智能存储插件）

这是社区插件，不是小米或阿里云盘官方发布。官方图标仅用于识别服务，不代表官方背书。

将 Xiaomi Smart Storage 的 `/nas/pool0` 文件夹与阿里云盘中选定的文件夹做单向备份。

## 功能

- NAS 上传备份到阿里云盘，或从阿里云盘下载备份到 NAS。
- 可手动执行，也可按每小时、每 6 小时或每天执行。
- 文件变化才会重复传输；同名但内容不同的下载文件会保留副本。
- 不同步删除：删除任务、解除授权或本地删除文件都不会删除另一端文件。
- 通过阿里云盘官方 OpenAPI 的 PKCE 二维码流程授权，不收集或保存账户密码。

## 前置条件

1. 在阿里云盘开放平台创建自用应用，并取得自己的 `App ID`。
2. 应用的 Bundle ID 设为 `com.xiaomi.smartstorage.aliyundrive.sync`。
3. 首次安装时 NAS 已有仅密钥认证的 root SSH，用于安装隔离的本机服务。

令牌和任务配置只保存在 NAS 的 `/data/plugin/aliyundrive-sync/data`，浏览器 API 不会返回令牌。

入口和 API 均使用当前小米客户端设备的同源 `/plugin/<用户>/aliyundrivesync/...` 路径，因此不依赖 NAS 的固定 IP。

## 校验

```bash
python3 -m py_compile server.py deploy/register_plugin.py
python3 -m unittest discover -s tests -v
node --check web/app.js
bash -n deploy/install-on-nas.sh
```

## 安装

```bash
cd xiaomi-aliyundrive-backup-0.1.0
NAS_IP="当前 NAS 局域网 IP" \
NAS_SSH_KEY="/绝对路径/root-ssh-private-key" \
bash deploy/install-on-nas.sh
```

安装器会在只有一个 `/data/plugin/u*.list` 时自动识别小米用户；多用户 NAS 需要额外设置 `NAS_USER_ID="u你的数字账号"`。默认插件编号是 `1002`。注册脚本会先检查冲突、为注册表创建受限备份，并且只写入自己的 `aliyundrivesync` 记录。

## 官方依据

- [阿里云盘开放平台应用接入说明](https://www.alibabacloud.com/help/zh/pds/drive-and-photo-service-dev/user-guide/application-access-details/)
- [阿里云盘官方 OpenSDK](https://github.com/alibaba/aliyunpan-android-sdk)
- [阿里云盘官方客户端下载页](https://www.aliyundrive.com/download)

`web/assets/aliyundrive-icon.png` 是阿里云盘官网公开的原始 512px 应用图标，未重绘或生成。
