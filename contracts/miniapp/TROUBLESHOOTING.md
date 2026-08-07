# 故障排查

| 现象 | 检查 | 处理 |
|---|---|---|
| 微信报不在合法域名 | 微信后台 request/upload/socket 配置、证书链、备案 | 使用正式 HTTPS/WSS 域名，不能配置 IP |
| 401 `TOKEN_EXPIRED` | Access Token 到期 | 用 Refresh Token 刷新一次，再重放原请求 |
| 401 `SESSION_REPLACED` | 同账号在另一设备登录 | 回到登录页，清理本地队列中的敏感缓存 |
| 401 `SESSION_TIMEOUT` | 30 分钟无操作 | 重新登录；保留未提交 outbox 等用户确认 |
| 409 `VERSION_CONFLICT` | 本地版本落后 | GET 最新对象，展示差异，禁止静默覆盖 |
| 409 `IDEMPOTENCY_CONFLICT` | 同键配了不同正文 | 这是客户端缺陷；新业务动作生成新键 |
| 扫码重试产生错误 | 幂等键随重试变化 | 同一次用户动作必须持久化并复用同一键 |
| WSS 重连后少事件 | cursor 未持久化 | 使用最后确认 cursor 重连，并 REST 拉当前任务 |
| 上传 415 | MIME、文件头或格式不符合 | 重新压缩成真实 JPG/PNG/WebP |
| 地图位置不动 | 任务状态、位置授权、后台省电、outbox | 仅 IN_TRANSIT 上报，检查待同步队列和最后时间 |
| 签收 409 | stop 未送达、版本旧、明细与任务不符 | 重新拉任务，按 `delivery_lines` 完整提交 |
| 日报重复 | 同店同日已有记录 | 展示已提交日报；需要修改时走后续审核流程 |

每次报障保留响应 `trace_id`、请求时间、账号角色、接口路径和幂等键，禁止记录 Token、二维码原文或微信 secret。
