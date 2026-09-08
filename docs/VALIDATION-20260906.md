# 发布前实机验证：2026-09-06

状态：候选验证中，尚未创建正式 Release。用户已允许自有 NAS 的逐项测试，要求先备份并保留回滚，不修改业务文件。

## 已验证

- 官方 PyPI 固定版本输入：17 项运行依赖、3 项构建依赖和离线 pip 引导文件。下载器只允许官方 HTTPS 主机，跳转前检查目标，断点续传后仍核对完整文件大小与 SHA-256。
- 固定官方 Python 构建镜像：`python@sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970`，ARM64、Python 3.12.14、glibc 2.36。
- oss2/crcmod 在断网、非 root、只读根文件系统、无 capabilities 的本机 Docker 容器内构建。只挂载构建输入、脚本、公共辅助模块和专用输出目录，没有挂载个人证书或签名密钥。
- 完整 17 项依赖通过哈希锁离线安装；真实 OSS SDK 向 loopback 夹具上传 1 MiB 内容并比对一致。
- RP05 实际 Python 3.12.4、glibc 2.39 上也通过断网离线安装，以及独立网络命名空间内非 root SDK 导入和同样的 1 MiB 模拟上传。

实机发现并修正：pip 仅位于 root 的用户 site-packages，`python -I -m pip` 无法使用。通过固定哈希的官方 pip wheel 直接运行安装器，不修改系统 Python、不启用用户 site-packages、不联网执行 get-pip。

## 对在线设备的影响

现有 115 插件程序、配置和 systemd unit 已备份在 NAS 私有验证目录，未下载或公开配置内容。依赖仅安装到独立测试目录，没有切换 current、修改注册信息、重启插件或读写业务文件。此时不需要执行回滚；备份保留供后续逐项升级验证。

## 可复用工具

- `scripts/prepare_115_inputs.py`：下载固定版本构建输入及来源清单，`--resume` 只复用哈希匹配的文件。
- `scripts/build_115_wheels.py`：容器内构建、生成锁文件、验证完整离线安装，输入 `/inputs`，输出 `/output`，公共模块 `/helpers`。
- `scripts/smoke_115_sdk.py`：使用真实 SDK 和仅本地的模拟 OSS 服务，不使用真实账号或上传业务文件。

运行时 wheel、pip 引导 wheel、锁文件和来源报告目前保存在仓库外的候选构建目录；未作为正式插件附件分发。构建脚本中复制所有依赖的路径已包含 `pip-bootstrap`，缺少或哈希不匹配时拒绝发布构建。

## 尚未完成

1. 依赖制品的分发扫描、许可证核对与最终签名包构建。
2. Mac/Android 的证书和 token/中继身份闭环，不恢复 loopback 免鉴权。
3. 安装持久化事务及中断恢复、特权边界审查。
4. 逐插件实际升级/回滚以及双端客户端验收。
5. 真实 Windows 10/11 安装验收，不能用 Mac 上的 PowerShell 参数夹具替代。

这里的 SDK 模拟上传不是 115 真实账号上传验收，更不是整套生态已正式发布。
