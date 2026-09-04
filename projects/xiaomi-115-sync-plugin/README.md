# 115 云备份（小米智能存储插件）

这是社区插件，不是小米或 115 官方发布。官方图标仅用于识别服务，不代表官方背书。

把小米智能存储中 `/nas/pool0` 下的一个文件夹，与一个 115 文件夹进行单向复制同步。

## 工作方式

- **上传备份**：NAS 文件夹 -> 115 文件夹。
- **下载备份**：115 文件夹 -> NAS 文件夹。
- 可选择仅手动、每小时、每 6 小时或每天执行。
- 同路径且内容相同的文件会跳过；同名但内容不同，会保留两份并为新副本添加来源和时间标记。
- 不实现镜像删除：删除任务或解除授权都不会删除 NAS 或 115 里的文件。
- 115 授权使用官方 OpenAPI 的 PKCE 扫码流程；插件不收集、不保存 115 账户密码。

## 前置条件

1. 在 [115 开放平台](https://open.115.com/) 完成开发者认证，并创建已获批准的应用，取得自己的 `App ID`。
2. NAS 已拥有仅密钥认证的 root SSH，用于首次安装插件服务。
3. NAS 能访问 115 OpenAPI 及 OSS 上传端点。

首次打开插件时，填入自己的 `App ID`，再使用 115 App 扫码授权。访问令牌只保存在 NAS 的 `/data/plugin/115-sync/data/config.json`，API 不会把令牌返回给浏览器。

## 目录

```text
server.py                         本机 115 OpenAPI 服务，监听 127.0.0.1:18115
web/                              小米客户端内嵌页面
web/assets/115-sync-icon.png      从 115 官方安卓客户端 APK 原样提取的 launcher 图标
deploy/register_plugin.py         安全写入小米插件注册表
deploy/install-on-nas.sh          需要明确执行的安装/更新脚本
```

应用入口和 API 都使用当前小米客户端已打开设备的同源 `/plugin/<用户>/115sync/...` 路径，不依赖某个固定 NAS IP。

## 本地校验

```bash
python3 -m py_compile server.py deploy/register_plugin.py
python3 -m unittest discover -s tests -v
node --check web/app.js
bash -n deploy/install-on-nas.sh
```

## 安装到 NAS

安装脚本会新建本插件自己的发布目录、systemd 服务、nginx 路由、图标和应用市场记录。它不触及 `miot_central`，也不修改其他插件或 NAS 文件。安装 Python 依赖前，脚本会从 NAS 侧对官方 PyPI、清华、阿里云、中科大和华为云镜像做小请求测速，自动使用最快可用源。

```bash
cd xiaomi-115-cloud-backup-0.1.0
NAS_IP="当前 NAS 局域网 IP" \
NAS_SSH_KEY="/绝对路径/root-ssh-private-key" \
bash deploy/install-on-nas.sh
```

安装器会在只有一个 `/data/plugin/u*.list` 时自动识别小米用户；多用户 NAS 需要额外设置 `NAS_USER_ID="u你的数字账号"`。`PLUGIN_ID` 默认使用 `1000`。若这个编号已被别的插件占用，注册脚本会拒绝继续而不是覆盖对方记录；可以显式设置一个空闲编号后重试。

## 官方接口依据

- [115 开放平台](https://open.115.com/)
- [PKCE 扫码授权](https://www.yuque.com/115yun/open/shtpzfhewv5nag11)
- [文件列表](https://www.yuque.com/115yun/open/kz9ft9a7s57ep868)
- [上传流程](https://www.yuque.com/115yun/open/xb89onhdxsfpwsyc)
- [下载地址](https://www.yuque.com/115yun/open/um8whr91bxb5997o)

品牌入口图标直接取自 115 官方安卓分发包 `115Life_38.2.0.apk` 的 `res/o-_.png`，未重绘、裁切或生成。该 APK 由 115 官网的版本接口返回：`https://appversion.115.com/1/web/1.0/api/getOneVerson?app_os=1`。
