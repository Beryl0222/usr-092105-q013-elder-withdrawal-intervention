"""校验领域事件信封与各类事件负载的公共约定。

仅依赖标准库；错误信息为可直接展示给接入方的中文。
业务规则（幂等、时限届满、授权门）不在此处，见 src/case_kernel.py。
"""

from datetime import datetime

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")

EVENT_TYPES = {
    # 原始五类（语义不变）
    "TRANSACTION_FLAGGED", "CONTACT_ATTEMPTED", "HOLD_APPLIED",
    "AUTHORITY_RESPONDED", "FUNDS_RELEASED",
    # 案件扩展
    "CASE_STATUS_CHANGED", "CUSTOMER_STATEMENT", "COMPANION_BEHAVIOR",
    "APPEAL_FILED",
    "ACCELERATION_REQUESTED", "ACCELERATION_GRANTED",
    "SIGNAL_RECORDED", "SIGNAL_WITHDRAWN",
    "HOLD_REVOKED", "HOLD_EXPIRED", "HOLD_EXTENDED",
    "SUPERVISOR_REVIEWED", "APPEAL_DECIDED",
    "PAYMENT_STOPPED", "DECISION_SUPERSEDED",
}

AGGREGATE_TYPES = {"withdrawal_case", "observed_signal", "protective_hold", "final_decision"}

# event_type -> 允许的 aggregate_type
EVENT_AGGREGATE = {
    "TRANSACTION_FLAGGED": "withdrawal_case",
    "CASE_STATUS_CHANGED": "withdrawal_case",
    "CUSTOMER_STATEMENT": "withdrawal_case",
    "COMPANION_BEHAVIOR": "withdrawal_case",
    "CONTACT_ATTEMPTED": "withdrawal_case",
    "APPEAL_FILED": "withdrawal_case",
    "ACCELERATION_REQUESTED": "withdrawal_case",
    "ACCELERATION_GRANTED": "withdrawal_case",
    "SIGNAL_RECORDED": "observed_signal",
    "SIGNAL_WITHDRAWN": "observed_signal",
    "HOLD_APPLIED": "protective_hold",
    "HOLD_REVOKED": "protective_hold",
    "HOLD_EXPIRED": "protective_hold",
    "HOLD_EXTENDED": "protective_hold",
    "SUPERVISOR_REVIEWED": "final_decision",
    "APPEAL_DECIDED": "final_decision",
    "AUTHORITY_RESPONDED": "final_decision",
    "FUNDS_RELEASED": "final_decision",
    "PAYMENT_STOPPED": "final_decision",
    "DECISION_SUPERSEDED": "final_decision",
}

# 各事件 data 内必填字段
DATA_REQUIRED = {
    "TRANSACTION_FLAGGED": ["channel", "requested_amount_ccy"],
    "CASE_STATUS_CHANGED": ["from_status", "to_status", "reason_code"],
    "CUSTOMER_STATEMENT": ["separate_setting", "in_own_words", "consistent"],
    "COMPANION_BEHAVIOR": ["behaviors"],
    "CONTACT_ATTEMPTED": ["target", "method", "result"],
    "APPEAL_FILED": ["appeal_code", "channel"],
    "ACCELERATION_REQUESTED": ["reason"],
    "ACCELERATION_GRANTED": ["granted", "approver"],
    "SIGNAL_RECORDED": ["grade", "code", "detail"],
    "SIGNAL_WITHDRAWN": ["withdrawn_reason"],
    "HOLD_APPLIED": ["reason_signal_refs", "deadline_key", "deadline_at", "approver", "customer_notified_at"],
    "HOLD_EXTENDED": ["prior_hold_ref", "reason_signal_refs", "deadline_key", "deadline_at", "approver"],
    "HOLD_REVOKED": ["source", "revoked_by"],
    "HOLD_EXPIRED": ["deadline_at", "responsible_role"],
    "SUPERVISOR_REVIEWED": ["decision", "reviewer", "basis_refs"],
    "APPEAL_DECIDED": ["appeal_code", "upheld", "reviewer"],
    "AUTHORITY_RESPONDED": ["authority_level"],
    "FUNDS_RELEASED": ["release_reason"],
    "PAYMENT_STOPPED": ["basis_type", "approver"],
    "DECISION_SUPERSEDED": ["superseded_ref", "new_ref", "reason"],
}

SIGNAL_GRADES = {"fact", "impression", "system_notice"}
AUTHORITY_LEVELS = {"L0", "L1", "L2", "L3"}
DEADLINE_KEYS = {"INITIAL_VERIFICATION", "SUPERVISOR_45MIN", "EXTENSION_4H", "AUTHORITY_48H", "APPEAL_2H"}


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def validate_event(record: dict) -> list[str]:
    """返回可以直接展示给接入方的中文错误；空列表表示通过。"""
    if not isinstance(record, dict):
        return ["事件必须是 JSON 对象"]

    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if errors:
        return errors  # 基础字段缺失时后续校验无意义

    if not isinstance(record["event_id"], str) or len(record["event_id"]) < 8:
        errors.append("event_id 必须是长度不少于 8 的字符串")
    if record["event_type"] not in EVENT_TYPES:
        errors.append(f"未知事件类型：{record['event_type']}")
    if record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{record['aggregate_type']}")
    if not isinstance(record["aggregate_id"], str) or not record["aggregate_id"]:
        errors.append("aggregate_id 必须是非空字符串")
    if not isinstance(record["version"], int) or isinstance(record["version"], bool) or record["version"] < 1:
        errors.append("version 必须是正整数")
    if not isinstance(record["summary"], str) or len(record["summary"]) < 2:
        errors.append("summary 必须是长度不少于 2 的中文字符串")
    if _parse_dt(record["occurred_at"]) is None:
        errors.append("occurred_at 必须是 RFC3339 时间，如 2026-09-20T12:00:00+08:00")
    if "received_at" in record and _parse_dt(record["received_at"]) is None:
        errors.append("received_at 必须是 RFC3339 时间")

    expected_aggregate = EVENT_AGGREGATE.get(record["event_type"])
    if expected_aggregate and record.get("aggregate_type") != expected_aggregate:
        errors.append(f"{record['event_type']} 必须归属聚合 {expected_aggregate}，当前为 {record.get('aggregate_type')}")

    errors.extend(_validate_data(record.get("event_type"), record.get("data")))
    return errors


def _validate_data(event_type: str, data) -> list[str]:
    required = DATA_REQUIRED.get(event_type)
    if not required:
        return []
    if not isinstance(data, dict):
        return [f"{event_type} 必须包含对象字段 data"]
    errors = [f"data 缺少字段：{name}" for name in required if name not in data]
    if errors:
        return errors

    if event_type in {"HOLD_APPLIED", "HOLD_EXTENDED"}:
        refs = data.get("reason_signal_refs", [])
        if not isinstance(refs, list) or not refs:
            errors.append("保护措施必须引用至少一条具体信号/联系记录（reason_signal_refs）")
        if data.get("deadline_key") not in DEADLINE_KEYS:
            errors.append("deadline_key 必须是设计文档 §8 的时限段之一")
        if _parse_dt(data.get("deadline_at", "")) is None:
            errors.append("deadline_at 必须是 RFC3339 时间")
    elif event_type in {"HOLD_EXPIRED"}:
        if _parse_dt(data.get("deadline_at", "")) is None:
            errors.append("deadline_at 必须是 RFC3339 时间")
    elif event_type == "HOLD_REVOKED":
        if data.get("source") not in {"appeal_upheld", "appeal_timeout", "supervisor_review", "authority_clear"}:
            errors.append("HOLD_REVOKED 的 source 不合法")
    elif event_type == "SIGNAL_RECORDED":
        if data.get("grade") not in SIGNAL_GRADES:
            errors.append("信号级别 grade 必须是 fact / impression / system_notice")
    elif event_type == "AUTHORITY_RESPONDED":
        level = data.get("authority_level")
        if level not in AUTHORITY_LEVELS:
            errors.append("authority_level 必须是 L0/L1/L2/L3")
        elif level in {"L2", "L3"} and not data.get("case_number"):
            errors.append("L2/L3 警方响应必须填写受案/文书编号 case_number")
    elif event_type == "FUNDS_RELEASED":
        if data.get("release_reason") not in {
            "verification_passed", "hold_expired", "appeal_upheld", "appeal_timeout",
            "supervisor_release", "customer_withdrew", "acceleration_clear",
        }:
            errors.append("release_reason 不是允许的放行原因")
    elif event_type == "PAYMENT_STOPPED":
        basis = data.get("basis_type")
        if basis == "customer_request" and not data.get("customer_request_ref"):
            errors.append("客户请求止付必须附书面/录音凭证 customer_request_ref")
        if basis == "legal_order" and not data.get("legal_case_number"):
            errors.append("法律文书止付必须填写文书编号 legal_case_number")
    elif event_type == "APPEAL_DECIDED":
        if not isinstance(data.get("upheld"), bool):
            errors.append("appeal 的 upheld 必须是布尔值")
    elif event_type == "SUPERVISOR_REVIEWED":
        if data.get("decision") not in {"release", "continue", "escalate"}:
            errors.append("主管复核 decision 必须是 release / continue / escalate")
        if not data.get("basis_refs"):
            errors.append("主管复核必须引用依据 basis_refs")
    return errors
