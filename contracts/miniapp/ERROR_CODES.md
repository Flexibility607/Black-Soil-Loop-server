# 错误码处理

| HTTP | code | 客户端处理 |
|---:|---|---|
| 400 | `QR_REQUIRED` / `QR_INVALID` | 保留页面，提示重新扫码 |
| 401 | `TOKEN_EXPIRED` | 刷新一次并重放原请求 |
| 401 | `SESSION_INVALID` / `SESSION_REPLACED` / `SESSION_TIMEOUT` | 清理登录态，回到登录页 |
| 403 | `FORBIDDEN` / `STORE_SCOPE_REQUIRED` | 显示无权限，不重试 |
| 404 | `TASK_NOT_FOUND` / `ALERT_NOT_FOUND` | 刷新列表，提示对象已不存在或无权查看 |
| 409 | `VERSION_CONFLICT` | 拉取最新对象，显示冲突，禁止静默覆盖 |
| 409 | `IDEMPOTENCY_CONFLICT` | 停止队列，该动作的键管理存在错误 |
| 409 | `INVALID_TRANSITION` | 刷新任务并按最新状态更新按钮 |
| 409 | `RECEIPT_LINES_MISMATCH` | 按 stop 的 `delivery_lines` 重建签收单 |
| 409 | `EXPECTED_QUANTITY_MISMATCH` | 禁止手改应收量，重新拉任务 |
| 413 | `UPLOAD_TOO_LARGE` | 压缩至 10 MB 以下 |
| 415 | `UPLOAD_TYPE_NOT_ALLOWED` / `UPLOAD_CONTENT_INVALID` | 转换为真实 JPG/PNG/WebP |
| 422 | `VALIDATION_ERROR` | 按 `details.errors` 标记表单字段 |
| 503 | `WECHAT_NOT_CONFIGURED` | 阻止生产发布并联系运维 |

所有错误上报保留 `trace_id`，界面显示中文 `message`；程序分支只依赖稳定的 `code`。
