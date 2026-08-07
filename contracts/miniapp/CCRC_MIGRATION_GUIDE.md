# CCRC 文件级改造手册

依据仓库当前 `main` 目录检查。改造应在 CCRC 自己的分支完成，本接口包不直接修改小程序仓库。

## 1. 应用与基础设施

### `app.js`

- 删除生产路径中的 `wx.cloud.init`、`cloudEnvId`、`useMock` 和写死的 `storeId`。
- 加载 `config/<env>.js`，在 `onLaunch` 调用 `wx.login` 并交给 `utils/api.js` 换取 B02 Token。
- `role/store_id/driver_id` 只使用 `/api/v1/mobile/auth/me` 返回值。
- 监听网络恢复并触发离线 outbox 重放；退出时清除 Token、角色和 WSS。

### 新增 `utils/api.js`

- 参考 `examples/request-client.example.js` 实现 `wx.request`、刷新锁、一次 401 重试、统一错误和 Trace ID。
- Access Token 仅放内存；Refresh Token 放受控本地缓存，退出时清除。
- 写操作必须显式传入 `Idempotency-Key` 与最新 `object_version`。

### `utils/bridge.js`

- 删除 `mockB01Data`、`mockSentSummaries`、`receiveFromB01` 和 `sendToB01`。
- 小程序只连接 B02。B01→B02 任务下发与 B02→B01 回传由服务器 outbox/Worker 完成，客户端不得自行双写。
- 文件可改为 WSS 订阅薄封装，参考 `examples/websocket.example.js`。

### `utils/cloud.js`

- 第一轮保留函数名作为迁移适配器，将 `transport/receipt/dailyReport/stock` 分发到 `utils/api.js`。
- 页面全部迁移后删除 `mockDB`、固定 2026-07 日期和随机数据；生产构建不得存在 Mock 默认分支。
- `cloudfunctions/transport`、`receipt`、`receive`、`dailyReport` 不再作为业务真相源，保留在历史分支即可。

### `utils/location.js`

- 将 30 秒自动上传改为 `POST /api/v1/mobile/tasks/{task_id}/locations`。
- `recorded_at` 使用 ISO 8601，`speed_mps` 保留米/秒，离线时把同一幂等键和正文放入 outbox。
- 任务离开 `IN_TRANSIT` 后停止持续定位。

## 2. 运输页面

### `pages/transport/transport.js`

- `loadTasks` 改为 `GET /api/v1/mobile/tasks`，直接保存 `id/task_no/status/object_version/stops`。
- 用 `STATUS_MAPPING.md` 替换本地 `STATUS_MAP`，增加接单、已取货和开始运输三个独立动作。
- 接单：`action=ACCEPT`；取货扫码：`action=PICKUP` 并传扫码得到的 `qr_token`；开始运输：`action=START_TRANSIT`。
- 送达：`action=DELIVER`，同时传 `stop_id` 与二维码原文。多站任务逐站操作。
- 每次成功后使用响应中的新 `object_version`；409 时重新拉任务，禁止本地自增版本。
- 报警从 `GET /api/v1/mobile/alerts` 获取，确认或解决时携带报警版本。
- 司机人工报告延误、货损、车辆故障等异常时调用 `POST .../exceptions`，不要把任务状态改成自由文本。

### `pages/transport/transport.wxml`

- 增加接单、取货、开始运输、逐站送达按钮和离线待同步标记。
- 显示经纬度估算路线、最后上报时间、温湿度报警状态；禁止写成实时道路导航。

## 3. 签收与库存

### `pages/receipt/receipt.js`

- 扫码原文保存在内存，任务详情从 `GET /api/v1/mobile/tasks/{task_id}` 获取。
- 使用当前门店 stop 的 `delivery_lines` 构建明细，不接受手填 `expected_quantity`。
- `R1/R2/R3` 改为 `FULL/PARTIAL/REJECTED`；提交 `POST .../receipts`，携带任务版本与幂等键。
- 签收成功已经在服务器单事务内生成库存流水，不再跳转 `scan-receive` 做第二次入库。

### `pages/scan-receive/scan-receive.js`

- 移除签收后的重复 `stockIn`。该页仅保留经过审批的独立库存调整入口；P0 可先改为只读库存流水。

### `pages/stock-list/stock-list.js`、`stock-detail/stock-detail.js`

- 列表改为 `GET /api/v1/mobile/inventory`。
- 显示服务器 `quantity/low_stock_threshold/object_version`，不再以云数据库 `_id` 为业务编号。

### `pages/stock-add/stock-add.js`、`stock-edit/stock-edit.js`

- 当前 B02 契约不允许店长直接覆盖库存余额。改为缺货上报或后续库存调整审批接口，禁止继续调用本地 `stock.save/update/remove`。

### `pages/restock/restock.js`

- 改为 `POST /api/v1/mobile/stockouts`，字段为 `product_id/requested_quantity/reason`，附带幂等键。
- 第三空间首页要把缺货上报作为一级入口。

## 4. 经营日报与第三空间

### `pages/daily-report/daily-report.js`

- 改为 `POST /api/v1/mobile/daily-reports`。
- 映射 `businessDate → report_date`、`salesAmount → sales_amount`、`orderCount → order_count`。
- 其他字段放入 `summary`，数据授权映射为 `authorized_for_dashboard`。
- 当日重复提交会返回 409；页面应展示已提交状态，不能生成第二条。

### `pages/index/index.js`、`app.json`

- 根据服务端角色突出三类入口：收货签收、缺货上报、销售经营日报。
- 第三空间角色进入首页时将这三个入口置顶；司机首页进入运输任务。

## 5. 附件、WSS 与清理

- 图片先通过 `POST /api/v1/mobile/uploads?purpose=...` 上传，保存返回的 `attachment.id` 到日报 `summary` 或异常材料。
- 连接 `wss://api.flexibility607.cn/api/v1/mobile/ws?token=...&cursor=...`；保存最后 cursor，断线指数退避并以 REST 补拉当前状态。
- `pages/generate-qr` 只能作为开发演示工具，正式构建不允许生成可用于生产任务的二维码。
- 完成迁移后，`utils/cloud.js` 的 Mock、业务云函数和固定日期从生产包移除；历史代码通过 Git 标签保留。
