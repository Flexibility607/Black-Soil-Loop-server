from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.dashboard import read_dashboard_projection
from app.domain.openai_layer import classify_intent
from app.shared.errors import BusinessError
from app.shared.models import AlgorithmRun


def answer_data_question(db: Session, question: str, preferred_chart: str = "auto") -> dict[str, Any]:
    snapshot = read_dashboard_projection(db)
    if snapshot is None:
        raise BusinessError("DASHBOARD_NOT_READY", "看板投影尚未生成", status_code=503)
    normalized = question.lower()
    intent = classify_intent(question)
    charts = snapshot["charts"]
    chart: dict[str, Any] | None = None

    if intent == "CHANNEL" or any(word in normalized for word in ["第三空间", "渠道", "占比"]):
        data = charts["channel_mix"]
        answer = f"第三空间销售额占比为 {snapshot['summary']['third_space_sales_share_pct']}%。"
        chart = {
            "type": "donut",
            "title": "渠道销售额占比",
            "data": data,
            "category_key": "channel",
            "value_key": "sales_amount",
        }
    elif intent == "ALERT" or any(word in normalized for word in ["温度", "湿度", "报警", "异常"]):
        data = snapshot["alerts"]
        answer = f"当前共有 {snapshot['summary']['open_alert_count']} 条未处理运输报警。"
        chart = {
            "type": "bar",
            "title": "运输报警",
            "data": [{"name": item["alert_type"], "value": 1} for item in data if item["status"] == "OPEN"],
            "category_key": "name",
            "value_key": "value",
        }
    elif intent == "OPERATIONS" or any(word in normalized for word in ["订单", "营业额", "销售"]):
        data = charts["daily_operations"]
        answer = f"近 14 天授权营业额合计 {snapshot['summary']['total_sales_amount']:.2f} 元。"
        chart = {
            "type": "line",
            "title": "每日订单与营业额",
            "data": data,
            "category_key": "date",
            "value_keys": ["order_count", "sales_amount"],
        }
    elif intent == "INVENTORY" or any(word in normalized for word in ["库存", "缺货"]):
        data = charts["inventory_by_product"]
        answer = f"当前有 {snapshot['summary']['low_stock_count']} 个门店商品库存低于阈值。"
        chart = {
            "type": "bar",
            "title": "商品库存汇总",
            "data": data,
            "category_key": "product_name",
            "value_key": "quantity",
        }
    elif intent == "TRANSPORT" or any(word in normalized for word in ["运输", "路线", "车辆"]):
        data = snapshot["map"]["routes"]
        answer = f"当前有 {snapshot['summary']['active_transport_count']} 条未完成运输任务，地图路线均为经纬度估算。"
        chart = {"type": "route", "title": "运输任务估算路线", "data": data}
    elif intent in {"CARPOOL", "WAREHOUSE", "PROCUREMENT", "FORECAST"} or any(
        word in normalized for word in ["拼车", "拼仓", "采购", "预测"]
    ):
        kind = (
            "DEMAND_FORECAST"
            if intent == "FORECAST" or "预测" in normalized
            else "PROCUREMENT"
            if intent == "PROCUREMENT" or "采购" in normalized
            else "WAREHOUSE"
            if intent == "WAREHOUSE" or "拼仓" in normalized
            else "CARPOOL"
        )
        latest = db.scalar(
            select(AlgorithmRun).where(AlgorithmRun.algorithm_type == kind).order_by(AlgorithmRun.created_at.desc())
        )
        answer = (
            "暂时没有对应的算法运行记录。"
            if latest is None
            else f"最近一次{kind}计算已生成，可查看规则解释和候选方案。"
        )
        chart = {
            "type": "bar",
            "title": "算法运行摘要",
            "data": latest.output_snapshot.get("candidates", [])[:8] if latest else [],
        }
    else:
        answer = (
            f"当前汇总覆盖 {snapshot['summary']['enterprise_count']} 家企业、"
            f"{snapshot['summary']['third_space_count']} 家第三空间，"
            f"授权销售额 {snapshot['summary']['total_sales_amount']:.2f} 元。"
        )
        chart = {
            "type": "bar",
            "title": "企业需求量",
            "data": charts["demand_by_enterprise"],
            "category_key": "name",
            "value_key": "quantity_kg",
        }

    if preferred_chart in {"bar", "line", "donut", "route"} and chart:
        chart["type"] = preferred_chart
    return {
        "answer": answer,
        "chart": chart,
        "mode": "openai_intent+deterministic" if intent else "deterministic",
        "allowed_chart_types": ["bar", "line", "donut", "route"],
        "data_cutoff": snapshot["data_cutoff"],
    }
