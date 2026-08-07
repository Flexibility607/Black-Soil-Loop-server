# 数据库迁移

生产迁移使用 `SERVICE_ROLE=migration` 和 `WORKER_DATABASE_URL`。部署账号不能直接连接数据库；迁移由受控的服务器发布脚本调用。

```bash
SERVICE_ROLE=migration .venv/bin/alembic upgrade head
```

正式环境禁止执行 `downgrade`。发生失败时回滚代码并提交向前修复迁移，部署前备份用于灾难恢复。
