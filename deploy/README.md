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

   脚本同时生成独立的 `VOICE_RATE_LIMIT_HMAC_SECRET`，并以 `VOICE_ASSISTANT_ENABLED=false`、`VOICE_PUBLIC_ENABLED=false`、`ASSISTANT_PUBLIC_DB_QUOTA_ENABLED=false` 写入语音安全默认值。阿里云 AccessKey、Secret、AppKey 和热词 ID 保持空值，必须由管理员在受限环境文件中手工填写，脚本不会读取或输出这些凭据。

5. 先启用 bootstrap Nginx 配置，在阿里云申请并下载两个独立的 Nginx 证书，再安装：

   ```bash
   install -d -o root -g root -m 700 /etc/black-soil-loop/certs
   install -o root -g root -m 644 api.flexibility607.cn.pem /etc/black-soil-loop/certs/
   install -o root -g root -m 600 api.flexibility607.cn.key /etc/black-soil-loop/certs/
   install -o root -g root -m 644 demo.flexibility607.cn.pem /etc/black-soil-loop/certs/
   install -o root -g root -m 600 demo.flexibility607.cn.key /etc/black-soil-loop/certs/
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

## 语音问答受控启用

语音发布必须按以下顺序执行，避免凭据或供应商故障影响现有大屏能力：

1. 保持三个开关均为 `false`，先发布代码并执行 Alembic `20260811_0012` 迁移和数据库权限刷新。
2. 在阿里云智能语音交互创建专用普通话 16 kHz 项目、最小权限 RAM 用户和业务热词词表。
3. 将 `ALIYUN_NLS_ACCESS_KEY_ID`、`ALIYUN_NLS_ACCESS_KEY_SECRET`、`ALIYUN_NLS_APP_KEY`、可选热词 ID 及独立 HMAC 写入 `/etc/black-soil-loop/backend.env`；文件继续保持 `0640 root:blacksoil`。不得把值粘贴到命令日志、工单或截图。
4. 先设置 `ASSISTANT_PUBLIC_DB_QUOTA_ENABLED=true` 和 `VOICE_ASSISTANT_ENABLED=true`，保持 `VOICE_PUBLIC_ENABLED=false`，重启 B01 后使用登录接口完成受控 WAV 转写烟测。
5. 核对阿里云调用次数、P95、429、5xx、数据库租约与审计元数据后，再设置 `VOICE_PUBLIC_ENABLED=true`。首个 24 小时建议把 `VOICE_GLOBAL_PER_DAY` 设为 `100`。
6. 发生供应商、费用或延迟异常时，仅将 `VOICE_PUBLIC_ENABLED=false` 并重启 B01；文字助手、预设问题、图表、SSE、B02 和数据库迁移继续保留。

凭据或 HMAC 缺失时，相关语音/配额端点返回 `503 TRANSCRIPTION_NOT_CONFIGURED`，B01 健康检查、公开文字助手和 SSE 仍可启动。阿里云开通与接口说明见[一句话识别](https://help.aliyun.com/zh/isi/developer-reference/api-reference-1)和[动态 Token](https://help.aliyun.com/zh/isi/getting-started/obtain-an-access-token)。

Nginx 的两个转写精确路径限制请求体并关闭请求缓冲，防止合法录音落入 `client_body_temp`。应用仅使用经可信 Nginx/Uvicorn 代理链规范化后的 `request.client.host` 生成 HMAC 主体；不得在应用层直接信任客户端提交的 `X-Forwarded-For` 或 `CF-Connecting-IP`。Cloudflare 地址段变化时应先从官方地址清单核验并更新 Nginx 配置。

## E02 展示受控启用

`20260813_0013` 只新增地图目录表和算法展示元数据，不重写历史门店、任务、位置或日报。部署代码时保持 `DASHBOARD_MAP_MODE=legacy`、`PUBLIC_INFORMATION_ENABLED=false`、`ALGORITHM_SHOWCASE_ENABLED=false`；迁移和角色授权完成后依次执行目录总校验、地图 dry-run/apply、预设场景 check/apply，再检查 7d、30d、month 三份投影。公开 DTO 通过 UUID 与禁止字段扫描、网页已兼容 v2 地图后，方可启用三个开关并仅重启 B01 与 Worker。

预设场景明确属于测算展示，只会创建派生 AlgorithmRun 和 Outbox。命令不得创建真实运输订单、确认方案、预占仓库、发布任务或修改库存。回滚时先恢复 `legacy/false/false` 并刷新三周期投影；0013 保持向前，不执行生产 downgrade。

## 固定演示数据库

`deploy/provision-showcase.sh` 首次创建 `black_soil_loop_showcase`、四个专用角色、root-only 配置与凭据文件，并安装演示 Worker 和每日 03:05 恢复定时器。脚本拒绝覆盖既有 `/etc/black-soil-loop/showcase.env`。服务器后续 release 若检测到该文件，会通过 `deploy/migrate-showcase.sh` 独立升级演示数据库并重新应用专用授权。

上线时必须先保持 `SHOWCASE_DATASET_ENABLED=false` 和 `PUBLIC_DASHBOARD_DATASET=live`，完成目录校验、案例安装、三周期投影与真实库不变性核验后再开启。回滚优先恢复 live/false 并停用演示 Worker 与定时器；迁移 `0014` 保持向前。完整步骤见 [固定演示案例运维手册](../../docs/fixed-demo-case-operations.md)。

## 验证

```bash
systemctl status blacksoil-b01 blacksoil-b02 blacksoil-worker
systemctl list-timers 'blacksoil-*'
curl --fail https://api.flexibility607.cn/health/b01
curl --fail https://api.flexibility607.cn/health/b02
nginx -t
```

## 阿里云证书更新

`api.flexibility607.cn` 与 `demo.flexibility607.cn` 使用两张独立的阿里云 Nginx 证书。下载包必须同时包含 PEM 和 KEY，替换前先核对 SAN、有效期与公私钥配对；私钥权限保持 `600 root:root`。更新后依次执行 `nginx -t`、`systemctl reload nginx`，并复查 API、备用网页和博客 HTTPS。

个人测试证书需要在到期前人工申请和替换。每周可执行以下命令检查剩余有效期：

```bash
openssl x509 -in /etc/black-soil-loop/certs/api.flexibility607.cn.pem -noout -checkend 1209600
openssl x509 -in /etc/black-soil-loop/certs/demo.flexibility607.cn.pem -noout -checkend 1209600
```
