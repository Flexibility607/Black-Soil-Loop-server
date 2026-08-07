# 阿里云部署手册

这些文件只管理黑土循环自己的账号、目录、systemd 单元和 Nginx 站点，不读取或改写 `/var/www/blog` 及博客站点配置。

## 首次准备

1. 备份现有 `/etc/nginx`、证书目录和博客部署配置。
2. DNS 确认 `api.flexibility607.cn`、`demo.flexibility607.cn` 均解析至 `47.94.3.39`。
3. 以 root 执行 `deploy/install-host.sh`，随后立即修改 `/etc/black-soil-loop/backend.env`。
4. 使用随机密码建立 PostgreSQL 角色并生成生产环境文件：

   ```bash
   deploy/provision-host.sh <服务器源码目录>
   ```

   脚本不会输出数据库、JWT 或设备密钥；演示管理员初始密码只写入 root 可读的 `/root/black-soil-loop-initial-credentials`。已配置环境会拒绝覆盖。

5. 先启用 bootstrap Nginx 配置，签发两个独立证书：

   ```bash
   certbot certonly --webroot -w /var/www/letsencrypt -d api.flexibility607.cn
   certbot certonly --webroot -w /var/www/letsencrypt -d demo.flexibility607.cn
   ```

6. 将 `deploy/nginx/black-soil-loop.conf` 安装为 `/etc/nginx/sites-available/black-soil-loop`，执行 `nginx -t` 后 reload。博客域名必须同时完成冒烟检查。

## 发布

CI 或本机先生成不含 `.git`、`.env`、数据库和缓存的 tar.gz 制品，上传后执行：

```bash
sudo deploy/deploy-release.sh <commit> <artifact.tar.gz>
```

脚本先加密备份，再执行 Alembic、权限刷新、软链接切换和双健康检查。健康检查失败会切回上一代码 release，数据库只允许向前修复。

网页仓库执行生产构建后，将 `dist` 目录内容打包在制品根目录并上传：

```bash
sudo deploy/deploy-web-release.sh <版本号> <web-dist.tar.gz>
```

网页发布脚本会拒绝包含 `mock/` 或未指向 `https://api.flexibility607.cn/api/v1` 的制品；切换后通过本机 HTTPS 冒烟检查，失败时恢复上一软链接。

## 验证

```bash
systemctl status blacksoil-b01 blacksoil-b02 blacksoil-worker
systemctl list-timers 'blacksoil-*'
curl --fail https://api.flexibility607.cn/health/b01
curl --fail https://api.flexibility607.cn/health/b02
nginx -t
```
