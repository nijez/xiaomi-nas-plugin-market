# NAS 离线依赖安装

状态：完整 ARM64 依赖已通过 RP05 隔离安装与 SDK 模拟上传，并纳入 0.1.3-beta.1 测试安装包。源码目录包含 wheelhouse、锁文件与来源报告，缺失或哈希不匹配时构建仍会明确失败。不要删除门禁或回退到 NAS 上联网 pip。

## 信任与速度

下载地址只负责运输，不能决定依赖内容。镜像测速应放在维护者准备发布物的阶段；无论哪个镜像最快，都必须匹配已审定的版本和 SHA-256。用户 NAS 安装的是签名插件包内的文件，不再逐个访问 Python 软件源。

签名插件包固定 `requirements.txt`、`requirements.lock` 和 `wheelhouse/`。锁定版本号不等于锁定文件；在下载现场自动生成哈希，也不等于验证发布者或上游身份。

参考 [pip 安全安装说明](https://pip.pypa.io/en/stable/topics/secure-installs/) 的哈希检查及禁用源码包要求。此实现还拒绝 wheel 元数据里的直接 URL 依赖，因为 `--no-index` 本身不阻止这类下载。

## 当前实现

- `shared/offline_dependencies.py` 仅接收严格的 `name==version`，拒绝 requirements 中的命令选项、URL、嵌套文件、重复包名。
- `requirements.lock` 每行是同一固定版本及至少一个 `--hash=sha256:<64 位小写十六进制>`。允许一个包携带多个平台 wheel，但每个文件哈希都必须匹配，不能包含未锁定或多余文件。
- wheel 必须是普通文件；禁止源码包、软链接、未知包及文件名与 METADATA 身份不一致。元数据检查不导入或执行依赖代码。
- 使用隔离 Python/pip，清除 pip/Python 环境覆盖并禁用配置文件。安装使用 `--no-index --only-binary=:all: --require-hashes --ignore-installed`，不加 `--no-deps`，由 pip 对目标 Python/平台和完整依赖关系继续校验。
- 在临时目录复制并再次验包，成功后才发布 `lib/`。失败删除本次临时目录，不修改既有 `lib/`。缺少 pip 时不会自动联网安装工具。
- RP05 的 pip 可能仅存在于 root 用户 site-packages，隔离 Python 不会加载。发布物可附带 `pip-bootstrap/pip-25.2-py3-none-any.whl`；模块核对代码中固定的官方 SHA-256 后，从暂存副本直接运行，不安装或升级系统 pip，不读取用户 site-packages。多余文件、软链接和错误哈希均拒绝。
- 市场安装、Windows/Mac 市场启动器以及 115 独立安装脚本接入同一模块。独立脚本先在电脑验包，再连接 NAS；在依赖准备成功前不替换已有 UI。独立脚本的完整服务事务迁移仍未完成。
- 发布构建在读取签名私钥、写出目录前检查依赖，不会签了一半才发现 115 缺包。

## 维护者准备步骤

1. 核对当前固定版本的官方文件清单、哈希、许可证及源码。2026-09-05 查询了当前 17 个 PyPI 固定版本，全部存在；[oss2 2.19.1](https://pypi.org/pypi/oss2/2.19.1/json) 和 [crcmod 1.7](https://pypi.org/pypi/crcmod/1.7/json) 没有 wheel。其余版本存在 ARM64 或纯 Python 候选，不等于已经通过 RP05 的 Python/ABI 兼容验收。
2. 对无 wheel 的依赖，在隔离、非特权的 Linux ARM64 构建环境中准备固定版本的构建工具和完整构建依赖，校验源码哈希后断网构建。不要在使用者的 NAS 上执行 `setup.py`，也不要把 Mac 编译产物直接发给 Linux。
3. 将审定的完整运行依赖 wheel 放入 `projects/xiaomi-115-sync-plugin/wheelhouse/`。在兼容的隔离环境验证依赖闭包、导入和 OSS SDK 上传调用。此步骤不能用文件名里含 `aarch64` 代替。
4. 在仓库根目录生成并复核锁文件：

```bash
python3 shared/offline_dependencies.py lock-reviewed-wheels \
  --requirements projects/xiaomi-115-sync-plugin/requirements.txt
python3 shared/offline_dependencies.py verify \
  --requirements projects/xiaomi-115-sync-plugin/requirements.txt
```

`lock-reviewed-wheels` 不下载、不证明来源、不自动更新既有锁文件，也不签名。依赖变更时必须单独审查新旧锁差异和上游证据。

5. 在目标兼容测试环境中，用 `install --requirements <路径> --target <全新临时目录>/lib` 验证离线安装，之后才进入 `BUILD.md` 的候选包构建和双端验收流程。正式签名与上传仍需要单独授权。

## 已验证与未覆盖

本机测试覆盖：哈希篡改、缺锁、缺包、版本漂移、选项注入、软链接、源码包、URL 依赖、身份错配、已有目标保护及失败清理。真实本机 pip 安装本地生成的纯 Python 测试 wheel 成功；缺少间接依赖或平台不匹配时失败且不发布 `lib/`。

2026-09-06：17 项运行依赖的完整离线安装在 ARM64 Python 3.12.14 容器和 RP05 Python 3.12.4 上通过。两处均使用真实 oss2 SDK 向本地 HTTP 夹具上传 1 MiB 文件并验证内容；RP05 运行时使用独立网络命名空间和非 root UID。NAS 原有插件未切换、未重启，配置与程序备份保留在 NAS。

这是 RP05 依赖兼容验收，不是实际 115 云账户上传、整插件升级或 Windows 实机验收。此模块不隔离恶意发布者、root 或同权限进程；已签名依赖仍然是可执行代码。WebDAV 已有 vendor 供应链、qB 镜像 digest 和 rclone 发布校验属于各自后续审查范围，没有被这次 wheel 门禁自动解决。
