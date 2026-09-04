# 精速 ERP 网页入口示例

此目录只提供网页代理入口的源码示例，不含 ERP 系统、数据库、公司内网地址或账户。尚未加入插件市场的一键安装列表。

入口仍由小米客户端定位当前 NAS；NAS 端仅向管理员配置的固定上游转发，不提供任意 URL 代理。上游系统自己的登录、权限和安全机制必须保留。

```bash
NAS_IP="当前 NAS IP" \
NAS_SSH_KEY="自己的 root 私钥绝对路径" \
NAS_USER_ID="u自己的数字账号" \
ERP_UPSTREAM_HOST="你的上游服务主机名或 IP" \
ERP_UPSTREAM_PORT=8080 \
bash deploy/install-on-nas.sh
```

安装前审阅代理范围及协议适配。这个示例曾针对特定 ERP 路由编写，不保证任意网站都能直接使用；请勿把不受认证保护的内部系统间接暴露给他人。

```bash
python3 -m unittest discover -s tests -v
bash -n deploy/install-on-nas.sh
```
