# 黑土循环服务器端

独立实现的 B01/B02 双服务单 PostgreSQL 平台。B01 服务网页、公开大屏、规划与算法；B02 服务司机、店长、第三空间、签收、库存、遥测与报警。两个服务共享身份和主数据，但通过数据库权限、投影视图与 outbox 保持写入边界。

## 本地启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m app.seed --reset
.\.venv\Scripts\python.exe -m uvicorn app.b01.main:app --port 8101
.\.venv\Scripts\python.exe -m uvicorn app.b02.main:app --port 8102
```

开发测试可将 `DATABASE_URL` 设置为 SQLite；正式环境必须使用 PostgreSQL。生产部署文件位于 `deploy/`，小程序对接包位于 `contracts/miniapp/`。

## 端点

- B01 OpenAPI：`http://127.0.0.1:8101/openapi.json`
- B02 OpenAPI：`http://127.0.0.1:8102/openapi.json`
- B01 健康检查：`/health/b01`
- B02 健康检查：`/health/b02`

## 数据规则

- 数据库存储 UTC，接口输出带时区 ISO 8601。
- 写请求携带 `object_version`；扫码、签收、日报、缺货等请求还需 `Idempotency-Key`。
- 业务状态、扫码、签收和库存流水保留不可变历史。
- 演示种子按执行时的上海日期生成，不导入旧数据库或 Mock。

## E02 匿名语音问答

语音功能默认关闭。启用后，浏览器上传 16 kHz、单声道、16 位 PCM WAV，B01 在内存中校验音频并通过阿里云智能语音交互一句话识别完成转写；转写文字继续进入本地白名单意图和固定业务 DTO，不向模型开放数据库、SQL、任意图表配置或写操作。

生产配置项如下。AccessKey、Secret、AppKey 和限流 HMAC 只能写入服务器受限环境文件，不能进入 Git、网页构建、日志、响应或截图。

```dotenv
VOICE_ASSISTANT_ENABLED=false
VOICE_PUBLIC_ENABLED=false
ASSISTANT_PUBLIC_DB_QUOTA_ENABLED=false
VOICE_STT_PROVIDER=aliyun
ALIYUN_NLS_ACCESS_KEY_ID=
ALIYUN_NLS_ACCESS_KEY_SECRET=
ALIYUN_NLS_APP_KEY=
ALIYUN_NLS_ENDPOINT=https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/asr
ALIYUN_NLS_VOCABULARY_ID=
VOICE_MAX_SECONDS=30
VOICE_MAX_BYTES=2097152
VOICE_PUBLIC_PER_MINUTE=3
VOICE_PUBLIC_PER_HOUR=20
VOICE_PUBLIC_PER_DAY=50
VOICE_PUBLIC_TEXT_PER_MINUTE=10
VOICE_PUBLIC_TEXT_PER_DAY=200
VOICE_GLOBAL_PER_DAY=500
VOICE_MAX_CONCURRENCY=2
VOICE_LEASE_SECONDS=60
VOICE_RATE_LIMIT_HMAC_SECRET=
```

需先在阿里云开通智能语音交互、创建普通话 16 kHz 项目并取得同一账号体系下的 AppKey 与动态 Token 权限。推荐为“吉品、黑土循环、第三空间、拼车、拼仓、缺货、签收、冷链、E01、E02、B01、B02”及实际商品、仓库、东北地名维护业务热词词表，并将词表 ID 写入 `ALIYUN_NLS_VOCABULARY_ID`。参考：[一句话识别 RESTful API](https://help.aliyun.com/zh/isi/developer-reference/restful-api-2)、[获取 Token](https://help.aliyun.com/zh/isi/getting-started/obtain-an-access-token)、[业务热词](https://help.aliyun.com/zh/isi/developer-reference/use-an-sdk-to-specify-the-id-of-a-business-specific-hotword-vocabulary)。

原始 WAV 不写入上传目录、数据库或对象存储。为跨进程幂等重放，完整转写仅保存在 B01 专用表中 10 分钟，并由每分钟清理任务删除；B02 无权读取该表。审计只保存匿名主体哈希、意图、周期、字节数、实际时长、耗时、状态、错误码和数据截止时间，不保存明文 IP、原始音频、完整问题、完整转写或阿里云上游响应。

登录转写接口仍允许省略 `duration_seconds`、`client_request_id` 与 `Idempotency-Key`，由服务器从 WAV 和 UUID 安全补齐；音频格式统一收敛为上述 PCM WAV。匿名接口要求表单 `duration_seconds`、`client_request_id` 与同值 UUID `Idempotency-Key`。

## 质量检查

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests scripts migrations
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m scripts.export_contracts
.\.venv\Scripts\python.exe -m scripts.check_contracts
.\.venv\Scripts\python.exe -m pytest tests/test_voice_assistant.py tests/test_postgres_permissions.py
```

GitHub Actions 使用 PostgreSQL 16，从空库连续执行两次 Alembic 升级，并验证 B01/B02 数据库角色边界。SQLite 只用于快速本地单元测试。

## 发布边界

- `deploy/` 只写入 `/srv/black-soil-loop*`、`/var/lib/black-soil-loop`、`/etc/black-soil-loop` 和独立 Nginx 站点文件。
- `deploy/deploy-release.sh` 发布服务器制品；`deploy/deploy-web-release.sh` 发布网页 `dist` 制品。
- 现有博客目录与站点配置不在脚本目标范围内，正式切换前后均需独立冒烟检查。
- `contracts/miniapp/` 是交付给 `X-BUGer/CCRC` 开发者的完整接口包，本仓库不直接修改小程序源码。
