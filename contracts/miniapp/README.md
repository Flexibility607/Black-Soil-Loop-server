# CCRC 小程序 B02 对接包

版本：`miniapp-contract-v0.1.0`。本目录面向 [X-BUGer/CCRC](https://github.com/X-BUGer/CCRC) 的后续改造开发者；本轮没有修改该仓库。

## 内容

- `b02-openapi.json`：B02 完整 OpenAPI。
- `schemas/`：移动任务、状态、列表、错误和 WSS 事件 JSON Schema。
- `config/`：开发、预发布、生产域名模板。
- `examples/`：微信登录、请求刷新、离线 outbox、WSS、GPS 和附件示例。
- `STATUS_MAPPING.md`：旧 T0–T4 与正式状态迁移。
- `ROLE_MATRIX.md`：司机、店长、第三空间和管理员权限。
- `CCRC_MIGRATION_GUIDE.md`：按实际文件路径列出的改造清单。
- `WECHAT_SETUP.md`：合法域名、权限、隐私和真机联调。
- `ACCEPTANCE.md`、`TROUBLESHOOTING.md`：验收与排障。

## 统一规则

- API 根地址：`https://api.flexibility607.cn`；正式小程序只使用 HTTPS/WSS 域名。
- Access Token 15 分钟，Refresh Token 7 天，30 分钟无操作会话失效。
- 写请求携带 `object_version`；扫码、签收、日报、缺货、轨迹和遥测携带稳定的 `Idempotency-Key`。
- `409 VERSION_CONFLICT` 时停止自动覆盖，重新获取对象并由用户确认。
- 同一幂等键与同一正文返回原响应；同键不同正文返回 `409 IDEMPOTENCY_CONFLICT`。
- 时间为带时区 ISO 8601；本地显示转换至 Asia/Shanghai。
- 运输路线统一显示为经纬度估算路线。

生成与校验：

```bash
python -m scripts.export_contracts
python -m scripts.check_contracts
```
