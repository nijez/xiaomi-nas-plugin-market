# 设备管家（小米智能存储社区插件）

在小米智能存储客户端中查看 NAS 的 CPU、内存、存储、温度、Docker 服务和运行中容器信息。

## 0.4.1

- 页面可见时每 2 秒刷新一次 CPU、内存、存储、温度、网络和磁盘 I/O。
- 实时采样窗口延长为 1 秒，减少短窗口漏掉磁盘写回的情况。
- 实时接口不读取 SMART、Docker 明细或历史曲线；SMART 保持 5 分钟缓存，Docker 统计保持 10 秒缓存。

## 0.4.0

- 新增活动物理网卡的实时接收、发送和总吞吐曲线。
- 新增物理数据盘的实时读取、写入和总吞吐曲线。
- 新增每块物理硬盘的型号、容量、温度、SMART 状态、通电时间及关键错误计数。
- SMART 结果缓存 5 分钟，并在硬盘待机时避免主动唤醒。
- Docker 容器详情新增 CPU、内存、网络 I/O、磁盘 I/O 和进程数。

## 0.3.0

- CPU 占用率改为读取 Linux `/proc/stat` 两次快照的累计时间差，不再用系统负载估算。
- 默认采样窗口为 250 毫秒；无法读取有效计数器时才回退到负载估算，并在 API 中标注数据来源。
- 保留每分钟一次、最多 1440 点的本地历史记录，用于最近 24 小时趋势图。

## 安全边界

- 状态采集服务只监听 NAS 的 `127.0.0.1:18080`，不直接暴露局域网端口。
- 页面通过小米客户端当前设备的同源 `/plugin/<用户>/devicemanager/` 入口访问，不绑定 NAS 固定 IP。
- 容器信息为只读；插件不启动、停止、删除或修改任何 Docker 容器。
- `miot_central` 始终标记为系统容器，只显示状态。
- 历史采样只写入 `/data/plugin/xiaomi-device-manager/history.json`。

这是社区插件，不是小米官方应用。安装前请确认设备归你所有并已配置仅密钥认证的 root SSH。

## 安装

在 Mac 或 Linux 电脑中解压发布包，然后执行：

```bash
NAS_IP="当前 NAS 局域网 IP" \
NAS_SSH_KEY="/绝对路径/root-ssh-private-key" \
bash deploy/install-on-nas.sh
```

安装器会在只有一个 `/data/plugin/u*.list` 时自动识别小米用户。多用户 NAS 需要额外设置 `NAS_USER_ID="u你的数字账号"`。默认插件编号为 `11001`；若发生冲突，可设置 `PLUGIN_ID` 为其他空闲正整数。

## 安装内容

- `/data/plugin/xiaomi-device-manager/`：服务版本与历史采样。
- `/home/<用户>/plugin/devicemanager/src/ui/`：客户端静态页面。
- `/etc/systemd/system/xiaomi-device-manager.service`：本机状态服务。
- `/etc/nginx/conf.d/luci/xiaomi-device-manager.conf`：同源插件入口。
- `/data/plugin/<用户>.list` 中仅新增或更新 `devicemanager` 记录，写入前会备份原注册表。

## 本地校验

```bash
python3 -m py_compile scripts/nas_status_server.py deploy/register_plugin.py
bash -n deploy/install-on-nas.sh
npm run check:runtime
npm run build
npm run test:sites
```
