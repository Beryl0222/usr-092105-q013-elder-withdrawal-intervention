"""本服务识别的事件类型、业务对象与载荷约定。

信封公共字段由 src.validator.validate_event 校验；本模块在其上补充
事件类型、业务对象匹配与业务载荷的校验。所有错误均为可直接展示的中文。
所有业务时限一律以信封 occurred_at（事实发生时间）为准，payload 不再
重复携带时间。
"""

from __future__ import annotations

from datetime import datetime

from src.validator import validate_event


class EventType:
    """领域事件类型。"""

    TRANSACTION_FLAGGED = "TRANSACTION_FLAGGED"                # 交易触发核实（仅触发，不否定交易能力）
    SELF_EXPRESSION_RECORDED = "SELF_EXPRESSION_RECORDED"      # 客户自主表达
    COMPANION_PROFILE_RECORDED = "COMPANION_PROFILE_RECORDED"  # 陪同关系
    CONTACT_ATTEMPTED = "CONTACT_ATTEMPTED"                    # 联系尝试
    SUPERVISOR_REVIEWED = "SUPERVISOR_REVIEWED"                # 主管复核
    SIGNAL_RECORDED = "SIGNAL_RECORDED"                        # 现场可观察信号
    SIGNAL_CONFIRMED = "SIGNAL_CONFIRMED"                      # 主观判断被事实确认
    SIGNAL_RETRACTED = "SIGNAL_RETRACTED"                      # 主观判断被撤回
    HOLD_APPLIED = "HOLD_APPLIED"                              # 保护性止付生效
    HOLD_EXTENDED = "HOLD_EXTENDED"                            # 保护性止付延期
    HOLD_LIFTED = "HOLD_LIFTED"                                # 保护性止付解除
    DECISION_RECORDED = "DECISION_RECORDED"                    # 最终决定（放行/止付）
    FUNDS_RELEASED = "FUNDS_RELEASED"                          # 支付系统执行放行
    APPEAL_FILED = "APPEAL_FILED"                              # 客户申诉
    APPEAL_REVIEWED = "APPEAL_REVIEWED"                        # 申诉复核结论
    AUTHORITY_RESPONDED = "AUTHORITY_RESPONDED"                # 警方响应


ALL_EVENT_TYPES = frozenset(
    getattr(EventType, name) for name in dir(EventType) if name.isupper()
)

# 每类业务对象允许承载的事件类型
AGGREGATE_EVENT_TYPES = {
    "withdrawal_case": frozenset({
        EventType.TRANSACTION_FLAGGED,
        EventType.SELF_EXPRESSION_RECORDED,
        EventType.COMPANION_PROFILE_RECORDED,
        EventType.CONTACT_ATTEMPTED,
        EventType.SUPERVISOR_REVIEWED,
        EventType.APPEAL_FILED,
        EventType.APPEAL_REVIEWED,
        EventType.AUTHORITY_RESPONDED,
    }),
    "observed_signal": frozenset({
        EventType.SIGNAL_RECORDED,
        EventType.SIGNAL_CONFIRMED,
        EventType.SIGNAL_RETRACTED,
    }),
    "protective_hold": frozenset({
        EventType.HOLD_APPLIED,
        EventType.HOLD_EXTENDED,
        EventType.HOLD_LIFTED,
    }),
    "final_decision": frozenset({
        EventType.DECISION_RECORDED,
        EventType.FUNDS_RELEASED,
    }),
}

# 触发原因：只用于启动核实，绝不能直接否定客户交易能力
TRIGGER_KINDS = ("age", "amount", "family_objection", "teller_observation", "system_rule")
TRIGGER_TEXT = {
    "age": "客户年龄因素",
    "amount": "交易金额达到关注标准",
    "family_objection": "家属提出异议",
    "teller_observation": "柜面现场观察",
    "system_rule": "系统风控规则",
}

CONTACT_RESULTS = ("reached", "no_answer", "blocked")
CHANNEL_TEXT = {"phone": "电话", "sms": "短信", "in_person": "现场"}
TARGET_TEXT = {"customer": "客户本人", "emergency_contact": "预留紧急联系人"}
RESULT_TEXT = {"reached": "已接通", "no_answer": "未接通", "blocked": "被阻断"}

REVIEW_CONCLUSIONS = ("concern_confirmed", "no_concern", "need_more_facts")
DECISION_OUTCOMES = ("release", "decline")
APPEAL_OUTCOMES = ("uphold", "overturn")
AUTHORITY_DISPOSITIONS = ("investigating", "fraud_confirmed", "no_fraud_confirmed")


def parse_time(value: object) -> datetime | None:
    """解析带时区的 ISO 8601 时间；非法或缺时区返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _require(payload: dict, fields: tuple[str, ...], errors: list[str]) -> None:
    for name in fields:
        if name not in payload or payload[name] is None or payload[name] == "":
            errors.append(f"payload 缺少字段：{name}")


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_flagged(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "customer_id", "amount", "trigger"), e)
    if "amount" in p and not (
        isinstance(p["amount"], (int, float))
        and not isinstance(p["amount"], bool)
        and p["amount"] > 0
    ):
        e.append("amount 必须为正数")
    trigger = p.get("trigger")
    if trigger is not None and (
        not isinstance(trigger, dict) or trigger.get("kind") not in TRIGGER_KINDS
    ):
        e.append(f"trigger.kind 必须为：{'、'.join(TRIGGER_KINDS)}（触发仅启动核实，不否定交易能力）")


def _validate_self_expression(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "statement", "expressed_independently"), e)
    if "expressed_independently" in p and not isinstance(p["expressed_independently"], bool):
        e.append("expressed_independently 必须为布尔值")


def _validate_companion(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "relationship"), e)
    for name in ("answers_for_customer", "rushing", "blocks_contact"):
        if name in p and not isinstance(p[name], bool):
            e.append(f"{name} 必须为布尔值")


def _validate_contact(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "channel", "target", "result"), e)
    if "channel" in p and p["channel"] not in CHANNEL_TEXT:
        e.append(f"channel 必须为：{'、'.join(CHANNEL_TEXT)}")
    if "target" in p and p["target"] not in TARGET_TEXT:
        e.append(f"target 必须为：{'、'.join(TARGET_TEXT)}")
    if "result" in p and p["result"] not in CONTACT_RESULTS:
        e.append(f"result 必须为：{'、'.join(CONTACT_RESULTS)}")


def _validate_review(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "reviewer_id", "reviewer_role", "conclusion", "rationale"), e)
    if "conclusion" in p and p["conclusion"] not in REVIEW_CONCLUSIONS:
        e.append(f"conclusion 必须为：{'、'.join(REVIEW_CONCLUSIONS)}")


def _validate_signal(p: dict, e: list[str]) -> None:
    # observation 为现场可观察事实；assessment 为主观标签，未经确认不得外流
    _require(p, ("case_id", "kind", "observation"), e)


def _validate_signal_outcome(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "basis"), e)


def _validate_hold_apply(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "reason", "duration_minutes", "escalation_owner_role", "decider_id", "decider_role"), e)
    if "duration_minutes" in p and not _is_positive_int(p["duration_minutes"]):
        e.append("duration_minutes 必须为正整数")


def _validate_hold_extend(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "reason", "duration_minutes", "escalation_owner_role", "approved_by"), e)
    if "duration_minutes" in p and not _is_positive_int(p["duration_minutes"]):
        e.append("duration_minutes 必须为正整数")


def _validate_hold_lift(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "basis", "actor_role"), e)


def _validate_decision(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "outcome", "basis", "decider_id", "decider_role"), e)
    if "outcome" in p and p["outcome"] not in DECISION_OUTCOMES:
        e.append(f"outcome 必须为：{'、'.join(DECISION_OUTCOMES)}")


def _validate_funds_released(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "payment_ref"), e)


def _validate_appeal_filed(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "filed_by", "channel", "grounds"), e)


def _validate_appeal_reviewed(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "reviewer_id", "reviewer_role", "outcome", "rationale"), e)
    if "outcome" in p and p["outcome"] not in APPEAL_OUTCOMES:
        e.append(f"outcome 必须为：{'、'.join(APPEAL_OUTCOMES)}")


def _validate_authority(p: dict, e: list[str]) -> None:
    _require(p, ("case_id", "authority", "reference", "disposition"), e)
    if "disposition" in p and p["disposition"] not in AUTHORITY_DISPOSITIONS:
        e.append(f"disposition 必须为：{'、'.join(AUTHORITY_DISPOSITIONS)}")


_PAYLOAD_VALIDATORS = {
    EventType.TRANSACTION_FLAGGED: _validate_flagged,
    EventType.SELF_EXPRESSION_RECORDED: _validate_self_expression,
    EventType.COMPANION_PROFILE_RECORDED: _validate_companion,
    EventType.CONTACT_ATTEMPTED: _validate_contact,
    EventType.SUPERVISOR_REVIEWED: _validate_review,
    EventType.SIGNAL_RECORDED: _validate_signal,
    EventType.SIGNAL_CONFIRMED: _validate_signal_outcome,
    EventType.SIGNAL_RETRACTED: _validate_signal_outcome,
    EventType.HOLD_APPLIED: _validate_hold_apply,
    EventType.HOLD_EXTENDED: _validate_hold_extend,
    EventType.HOLD_LIFTED: _validate_hold_lift,
    EventType.DECISION_RECORDED: _validate_decision,
    EventType.FUNDS_RELEASED: _validate_funds_released,
    EventType.APPEAL_FILED: _validate_appeal_filed,
    EventType.APPEAL_REVIEWED: _validate_appeal_reviewed,
    EventType.AUTHORITY_RESPONDED: _validate_authority,
}


def validate_envelope(envelope: dict) -> list[str]:
    """校验信封公共字段、事件类型与业务对象匹配、payload 载荷。"""
    errors = validate_event(envelope)
    event_type = envelope.get("event_type")
    aggregate_type = envelope.get("aggregate_type")
    if event_type is not None and event_type not in ALL_EVENT_TYPES:
        errors.append(f"未知事件类型：{event_type}")
    if aggregate_type is not None and aggregate_type not in AGGREGATE_EVENT_TYPES:
        errors.append(f"未知业务对象类型：{aggregate_type}")
    if event_type in ALL_EVENT_TYPES and aggregate_type in AGGREGATE_EVENT_TYPES:
        if event_type not in AGGREGATE_EVENT_TYPES[aggregate_type]:
            errors.append(f"事件 {event_type} 不能挂在业务对象 {aggregate_type} 上")
    if "occurred_at" in envelope and parse_time(envelope.get("occurred_at")) is None:
        errors.append("occurred_at 必须是带时区的 ISO 8601 时间")
    if event_type in ALL_EVENT_TYPES:
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            errors.append("缺少 payload 对象")
        else:
            _PAYLOAD_VALIDATORS[event_type](payload, errors)
    return errors
