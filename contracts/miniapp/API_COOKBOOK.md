# 核心接口示例

## 取货扫码

```json
POST /api/v1/mobile/tasks/{task_id}/actions
Idempotency-Key: pickup-本地持久键

{
  "action": "PICKUP",
  "object_version": 3,
  "qr_token": "扫码得到的完整原文"
}
```

## 逐站送达

```json
{
  "action": "DELIVER",
  "object_version": 6,
  "stop_id": "站点 UUID",
  "qr_token": "扫码得到的完整原文",
  "occurred_at": "2026-08-06T10:30:00+08:00"
}
```

## 全量签收

`expected_quantity` 必须原样来自任务 stop 的 `delivery_lines`。

```json
POST /api/v1/mobile/tasks/{task_id}/receipts
Idempotency-Key: receipt-本地持久键

{
  "object_version": 8,
  "receipt_status": "FULL",
  "qr_token": "扫码得到的完整原文",
  "lines": [
    {
      "product_id": "商品 UUID",
      "expected_quantity": 48,
      "received_quantity": 48,
      "note": null
    }
  ]
}
```

## 第三空间缺货上报

```json
POST /api/v1/mobile/stockouts
Idempotency-Key: stockout-本地持久键

{
  "product_id": "商品 UUID",
  "requested_quantity": 30,
  "reason": "周末客流增加，当前库存不足"
}
```

## 经营日报

```json
POST /api/v1/mobile/daily-reports
Idempotency-Key: daily-门店-日期

{
  "report_date": "2026-08-06",
  "sales_amount": 8650.00,
  "order_count": 48,
  "authorized_for_dashboard": true,
  "summary": {
    "sales_quantity": 156,
    "wastage": 3,
    "attachment_ids": []
  }
}
```
