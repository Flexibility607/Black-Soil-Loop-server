from __future__ import annotations

from typing import Final

MODULE_LABELS: Final = {
    "B01": "网页管理与经营服务",
    "B02": "移动履约与设备服务",
    "E01": "园区管理台",
    "E02": "公开产销协同大屏",
}

ROLE_LABELS: Final = {
    "park_admin": "园区管理员",
    "enterprise_admin": "企业管理员",
    "driver": "运输司机",
    "store_manager": "传统门店店长",
    "third_space_manager": "第三空间店长",
}

TRANSPORT_STATUS_LABELS: Final = {
    "DRAFT": "草稿",
    "MATCHED": "已匹配",
    "CONFIRMED": "已确认",
    "READY": "执行任务已就绪",
    "PUBLISH_REQUESTED": "发布处理中",
    "PUBLISHED": "已发布",
    "DRIVER_ACCEPTED": "司机已接单",
    "PICKED_UP": "已取货",
    "IN_TRANSIT": "运输中",
    "DELIVERED": "已送达",
    "STORE_SIGNED": "门店已签收",
    "SIGNED": "已签收",
    "PLANNED": "待送达",
    "COMPLETED": "已完成",
    "CANCELLATION_REQUESTED": "取消处理中",
    "CANCELLATION_REJECTED": "取消申请被拒绝",
    "CANCELLED": "已取消",
}

TRANSPORT_ACTION_LABELS: Final = {
    "ACCEPT": "司机接单",
    "PICKUP": "扫码取货",
    "START_TRANSIT": "开始运输",
    "DELIVER": "送达站点",
    "CANCEL": "取消任务",
    "PUBLISH": "发布任务",
    "SIGN_RECEIPT": "门店签收",
    "RECEIPT_FULL": "全量签收",
    "RECEIPT_PARTIAL": "差异签收",
    "RECEIPT_REJECTED": "拒收",
    "AUTO_COMPLETE": "自动完成任务",
    "ACKNOWLEDGE": "确认报警",
    "RESOLVE": "处理报警",
}

ALERT_TYPE_LABELS: Final = {
    "TEMPERATURE": "温度异常",
    "HUMIDITY": "湿度异常",
    "TEMPERATURE+HUMIDITY": "温湿度异常",
    "DELAY": "运输延误",
    "DAMAGE": "货损异常",
    "VEHICLE": "车辆异常",
    "OTHER": "其他异常",
}

ALERT_STATUS_LABELS: Final = {
    "OPEN": "待处理",
    "ACKNOWLEDGED": "已确认",
    "RESOLVED": "已处理",
}

ALGORITHM_TYPE_LABELS: Final = {
    "CARPOOL": "拼车匹配",
    "WAREHOUSE": "拼仓匹配",
    "PROCUREMENT": "采购推荐",
    "DEMAND_FORECAST": "需求预测",
}

FORECAST_METHOD_LABELS: Final = {
    "SEASONAL_EXPONENTIAL_SMOOTHING": "季节性指数平滑",
    "WEIGHTED_MOVING_AVERAGE": "加权移动平均",
    "PRODUCTION_PLAN_FALLBACK": "生产计划降级预测",
    "INSUFFICIENT_DATA": "数据不足",
    "NO_DATA": "无历史数据",
}

TEMPERATURE_ZONE_LABELS: Final = {"AMBIENT": "常温", "CHILLED": "冷藏", "FROZEN": "冷冻"}
CHANNEL_LABELS: Final = {
    "TRADITIONAL": "传统门店",
    "TRADITIONAL_STORE": "传统门店",
    "THIRD_SPACE": "第三空间",
}
RECEIPT_STATUS_LABELS: Final = {"FULL": "全量签收", "PARTIAL": "差异签收", "REJECTED": "拒收"}
STOCKOUT_STATUS_LABELS: Final = {
    "SUBMITTED": "已提交",
    "PROCESSING": "处理中",
    "FULFILLED": "已满足",
    "CANCELLED": "已取消",
}
MOVEMENT_TYPE_LABELS: Final = {"IN": "入库", "OUT": "出库", "ADJUST": "库存调整"}
INVENTORY_QUANTITY_CONTEXT_LABELS: Final = {
    "RECORDED": "业务写入时记录",
    "RECONSTRUCTED_FROM_BALANCE": "依据库存余额和流水顺序回推",
    "UNKNOWN_NO_BALANCE": "历史库存余额缺失，期初期末数量未知",
    "UNKNOWN_INCONSISTENT_HISTORY": "历史流水不一致，期初期末数量未知",
    "UNKNOWN": "历史数量语义待核验",
}
TELEMETRY_ISSUE_TYPE_LABELS: Final = {
    "ANOMALOUS_TELEMETRY": "温湿度遥测异常",
    "TELEMETRY_TASK_STATE_MISMATCH": "遥测上传与任务状态不一致",
    "DEVICE_VEHICLE_MISMATCH": "设备车辆与任务车辆不一致",
    "LOCATION_TASK_STATE_MISMATCH": "位置上传与任务状态不一致",
    "DRIVER_TASK_MISMATCH": "上报司机与任务司机不一致",
}
TELEMETRY_ISSUE_STATUS_LABELS: Final = {"OPEN": "待核查", "RESOLVED": "已处理"}
WAREHOUSE_STATUS_LABELS: Final = {
    "DRAFT": "草稿",
    "RESERVATION_REQUESTED": "预占处理中",
    "RESERVED": "已预占",
    "OCCUPY_REQUESTED": "占用处理中",
    "OCCUPIED": "已占用",
    "RELEASE_REQUESTED": "释放处理中",
    "RELEASED": "已释放",
    "CANCELLATION_REQUESTED": "取消处理中",
    "CANCELLED": "已取消",
    "EXPIRED": "已过期",
    "REJECTED": "不符合规则",
}
PROCUREMENT_SOURCE_LABELS: Final = {
    "MANUAL_CONFIRMED": "人工确认需求",
    "FORECAST": "预测需求",
    "PRODUCTION_PLAN": "生产计划",
}
SEVERITY_LABELS: Final = {"INFO": "提示", "WARNING": "警告", "CRITICAL": "严重"}


def enum_label(mapping: dict[str, str], value: str) -> str:
    return mapping.get(value, f"未知类型（{value}）")


def dictionary_payload() -> dict[str, dict[str, str] | str]:
    return {
        "modules": MODULE_LABELS,
        "roles": ROLE_LABELS,
        "transport_status": TRANSPORT_STATUS_LABELS,
        "transport_action": TRANSPORT_ACTION_LABELS,
        "alert_type": ALERT_TYPE_LABELS,
        "alert_status": ALERT_STATUS_LABELS,
        "algorithm_type": ALGORITHM_TYPE_LABELS,
        "forecast_method": FORECAST_METHOD_LABELS,
        "temperature_zone": TEMPERATURE_ZONE_LABELS,
        "channel": CHANNEL_LABELS,
        "receipt_status": RECEIPT_STATUS_LABELS,
        "stockout_status": STOCKOUT_STATUS_LABELS,
        "movement_type": MOVEMENT_TYPE_LABELS,
        "inventory_quantity_context": INVENTORY_QUANTITY_CONTEXT_LABELS,
        "telemetry_issue_type": TELEMETRY_ISSUE_TYPE_LABELS,
        "telemetry_issue_status": TELEMETRY_ISSUE_STATUS_LABELS,
        "warehouse_status": WAREHOUSE_STATUS_LABELS,
        "procurement_source": PROCUREMENT_SOURCE_LABELS,
        "severity": SEVERITY_LABELS,
        "unknown_fallback": "未知类型（原值）",
    }
