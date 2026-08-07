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

## 质量检查

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests scripts migrations
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m scripts.export_contracts
.\.venv\Scripts\python.exe -m scripts.check_contracts
```

GitHub Actions 使用 PostgreSQL 16，从空库连续执行两次 Alembic 升级，并验证 B01/B02 数据库角色边界。SQLite 只用于快速本地单元测试。

## 发布边界

- `deploy/` 只写入 `/srv/black-soil-loop*`、`/var/lib/black-soil-loop`、`/etc/black-soil-loop` 和独立 Nginx 站点文件。
- `deploy/deploy-release.sh` 发布服务器制品；`deploy/deploy-web-release.sh` 发布网页 `dist` 制品。
- 现有博客目录与站点配置不在脚本目标范围内，正式切换前后均需独立冒烟检查。
- `contracts/miniapp/` 是交付给 `X-BUGer/CCRC` 开发者的完整接口包，本仓库不直接修改小程序源码。
