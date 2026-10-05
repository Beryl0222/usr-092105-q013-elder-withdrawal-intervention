"""涉老取款紧急干预——幂等案件内核。

职责：
1. 幂等接入网点/支付系统反复通知的事件（event_id + 自然键去重，版本冲突隔离，保留 occurred_at）；
2. 从只追加事件流推导案件状态；
3. 时限届满时机械执行“无有效决定默认放行”，申诉超时自动解除；
4. 提供四类只读视图：柜员（倒计时/责任人/下一动作）、客户书面理由、跨业务外溢隔离、警方授权披露门。

纯标准库、无 IO 依赖；存储由调用方传入（默认内存实现，可换成数据库适配器）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import count

from .validator import validate_event

# ---- §8 时限（分钟），分行可调，调参留痕 ------------------------------------
POLICY_MINUTES = {
    "INITIAL_VERIFICATION": 15,
    "SUPERVISOR_45MIN": 45,
    "EXTENSION_4H": 4 * 60,
    "AUTHORITY_48H": 48 * 60,
    "APPEAL_2H": 2 * 60,
}
# 现场申诉力争 30 分钟内换人完成（软目标，独立于 2h 硬时限）
SOFT_APPEAL_TARGET_MIN = 30
# 加速条款（§8）：前三段时限系数 0.5
ACCELERATED_KEYS = {"INITIAL_VERIFICATION", "SUPERVISOR_45MIN", "EXTENSION_4H"}

REL_DECISION_EVENTS = {"SUPERVISOR_REVIEWED", "APPEAL_DECIDED", "AUTHORITY_RESPONDED", "PAYMENT_STOPPED"}


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _canonical(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---- 事件存储 ---------------------------------------------------------------

@dataclass
class AppendResult:
    status: str            # accepted / duplicate / invalid / conflict
    detail: str = ""
    ref: str = ""         # accepted/duplicate 时对应事件的 event_id


class EventStore:
    """按案件分组的只追加事件存储。内存实现；生产环境替换为 DB 适配器即可。"""

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._by_event_id: dict[str, dict] = {}
        self._duplicates = 0
        self._conflicts = 0

    @property
    def metrics(self) -> dict:
        return {"events": len(self._events), "duplicates": self._duplicates, "conflicts": self._conflicts}

    def append(self, event: dict) -> AppendResult:
        errors = validate_event(event)
        if errors:
            return AppendResult("invalid", "；".join(errors))

        case_id = event.get("case_id") or (
            event["aggregate_id"] if event["aggregate_type"] == "withdrawal_case" else None
        )
        if not case_id:
            return AppendResult("invalid", f"{event['event_type']} 必须回填 case_id")
        event = {**event, "case_id": case_id}

        # 1) event_id 去重：重试必须沿用原 ID，内容不同即冲突
        prior = self._by_event_id.get(event["event_id"])
        if prior is not None:
            if _canonical(prior.get("data")) == _canonical(event.get("data")):
                self._duplicates += 1
                return AppendResult("duplicate", "同一 event_id 的重复通知，已忽略", event["event_id"])
            self._conflicts += 1
            return AppendResult("conflict", f"event_id {event['event_id']} 内容与既有事件不一致，进异常队列")

        # 2) 同聚合同 version 内容必须一致（乱序到达允许，但不能改写历史）
        for old in self._events:
            if old["aggregate_id"] == event["aggregate_id"] and old["version"] == event["version"]:
                if _canonical(old.get("data")) == _canonical(event.get("data")):
                    self._duplicates += 1
                    return AppendResult("duplicate", "同聚合同版本的重复通知，已忽略", event["event_id"])
                self._conflicts += 1
                return AppendResult("conflict", f"{event['aggregate_id']} version {event['version']} 内容冲突")

        # 3) 自然键去重：换 event_id 的重发（同对象/同类型/同发生时间/同内容）
        nat = (event["aggregate_id"], event["event_type"], event["occurred_at"], _canonical(event.get("data")))
        for old in self._events:
            if (old["aggregate_id"], old["event_type"], old["occurred_at"], _canonical(old.get("data"))) == nat:
                self._duplicates += 1
                return AppendResult("duplicate", "自然键一致的重复通知，已忽略", old["event_id"])

        self._events.append(event)
        self._by_event_id[event["event_id"]] = event
        return AppendResult("accepted", ref=event["event_id"])

    def events_for_case(self, case_id: str) -> list[dict]:
        # 时间轴一律按 occurred_at；同刻按 version 稳定排序
        return sorted(
            (e for e in self._events if e["case_id"] == case_id),
            key=lambda e: (e["occurred_at"], e["aggregate_type"], e["version"]),
        )

    def cases(self) -> set[str]:
        return {e["case_id"] for e in self._events}


# ---- 案件推导 ---------------------------------------------------------------

@dataclass
class HoldState:
    hold_id: str
    deadline_key: str
    deadline_at: datetime
    approver: str
    reason_refs: list[str]
    opened_at: datetime
    closed: str | None = None        # revoked / expired / None
    accelerated: bool = False


@dataclass
class CaseState:
    case_id: str
    customer_id: str | None
    opened_at: datetime
    status: str
    active_hold: HoldState | None
    appeal: dict | None
    authority_level: str
    signals: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


class CaseProjector:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def state(self, case_id: str, now: datetime) -> CaseState | None:
        events = [e for e in self.store.events_for_case(case_id) if parse_dt(e["occurred_at"]) <= now]
        if not events:
            return None
        flagged = next((e for e in events if e["event_type"] == "TRANSACTION_FLAGGED"), None)
        if flagged is None:
            return None
        t0 = parse_dt(flagged["occurred_at"])

        holds = self._holds(events)
        signals = [e for e in events if e["event_type"] == "SIGNAL_RECORDED"]
        authority = self._latest(events, "AUTHORITY_RESPONDED")
        authority_level = authority["data"]["authority_level"] if authority else "L0"

        appeal = None
        filed = self._latest(events, "APPEAL_FILED")
        if filed:
            decided = self._latest_after(events, "APPEAL_DECIDED", filed["occurred_at"])
            deadline = parse_dt(filed["occurred_at"]) + timedelta(minutes=POLICY_MINUTES["APPEAL_2H"])
            appeal = {
                "code": filed["data"]["appeal_code"],
                "filed_at": filed["occurred_at"],
                "deadline": deadline,
                "decided": decided,
                "reviewer": (decided["data"]["reviewer"] if decided else None),
            }

        released = self._latest(events, "FUNDS_RELEASED")
        stopped = self._latest(events, "PAYMENT_STOPPED")
        active_hold = next((h for h in reversed(holds) if h.closed is None), None)

        if stopped:
            status = "STOP_PAYMENT"
        elif released:
            status = "CLOSED_RELEASED"
        elif appeal and appeal["decided"] is None and active_hold is not None:
            status = "PENDING_APPEAL"
        elif active_hold is not None:
            status = "HELD"
        else:
            activity = {"SIGNAL_RECORDED", "CONTACT_ATTEMPTED", "CUSTOMER_STATEMENT", "COMPANION_BEHAVIOR"}
            status = "IN_VERIFICATION" if any(e["event_type"] in activity for e in events) else "OPENED"

        return CaseState(
            case_id=case_id,
            customer_id=flagged.get("data", {}).get("customer_id"),
            opened_at=t0,
            status=status,
            active_hold=active_hold,
            appeal=appeal,
            authority_level=authority_level,
            signals=signals,
            events=events,
        )

    # -- 持有措施链 --
    @staticmethod
    def _holds(events: list[dict]) -> list[HoldState]:
        holds: dict[str, HoldState] = {}
        order: list[str] = []
        for e in events:
            et, d, ts = e["event_type"], e.get("data") or {}, parse_dt(e["occurred_at"])
            if et == "HOLD_APPLIED":
                h = HoldState(
                    hold_id=e["aggregate_id"], deadline_key=d["deadline_key"],
                    deadline_at=parse_dt(d["deadline_at"]), approver=d["approver"],
                    reason_refs=list(d["reason_signal_refs"]), opened_at=ts,
                    accelerated=bool(d.get("accelerated")),
                )
                holds[h.hold_id] = h
                order.append(h.hold_id)
            elif et == "HOLD_EXTENDED":
                parent = holds.get(d["prior_hold_ref"])
                if parent is not None and parent.closed is None:
                    parent.closed = "extended"
                h = HoldState(
                    hold_id=e["aggregate_id"], deadline_key=d["deadline_key"],
                    deadline_at=parse_dt(d["deadline_at"]), approver=d["approver"],
                    reason_refs=list(d["reason_signal_refs"]), opened_at=ts,
                    accelerated=parent.accelerated if parent else False,
                )
                holds[h.hold_id] = h
                order.append(h.hold_id)
            elif et == "HOLD_REVOKED":
                self_id = e["aggregate_id"]
                if self_id in holds:
                    holds[self_id].closed = "revoked"
            elif et == "HOLD_EXPIRED":
                if e["aggregate_id"] in holds:
                    holds[e["aggregate_id"]].closed = "expired"
            elif et == "FUNDS_RELEASED":
                for hid in order:
                    if holds[hid].closed is None:
                        holds[hid].closed = "expired" if d.get("release_reason") in {"hold_expired", "appeal_timeout"} else "revoked"
            elif et == "PAYMENT_STOPPED":
                for hid in order:
                    if holds[hid].closed is None:
                        holds[hid].closed = "stopped"
        return [holds[h] for h in order]

    @staticmethod
    def _latest(events, etype):
        return next((e for e in reversed(events) if e["event_type"] == etype), None)

    @staticmethod
    def _latest_after(events, etype, after_ts):
        return next((e for e in reversed(events)
                     if e["event_type"] == etype and e["occurred_at"] >= after_ts), None)


# ---- 届满扫描：无有效决定默认放行（§10.3） -----------------------------------

class Sweeper:
    def __init__(self, store: EventStore, id_gen=None) -> None:
        self.store = store
        self._seq = count(1)
        self.id_gen = id_gen or (lambda prefix: f"{prefix}-{next(self._seq):06d}")

    def sweep(self, now: datetime) -> list[dict]:
        """扫描所有未结案件，生成届满/超时事件并幂等写回。返回新写入的事件。

        可重复调用：系统生成事件在重复扫描时被自然键幂等忽略。
        """
        proj = CaseProjector(self.store)
        created: list[dict] = []
        for case_id in self.store.cases():
            state = proj.state(case_id, now)
            if state is None or state.status in {"CLOSED_RELEASED", "STOP_PAYMENT"}:
                continue

            # 1) 申诉 2h 未决 → 解除措施并放行（§11），措施不因申诉而延长
            appeal = state.appeal
            if appeal and appeal["decided"] is None and now >= appeal["deadline"] and state.active_hold:
                created += self._close_with_release(
                    state, appeal["deadline"], "appeal_timeout",
                    close_hold=("HOLD_REVOKED",
                                {"source": "appeal_timeout", "revoked_by": "system"},
                                "申诉 2 小时未决，措施自动解除"),
                    note="申诉 2 小时未决，措施自动解除",
                )
                continue

            # 2) 保护措施届满（§10.3）：截至截止时刻最新有效决定
            hold = state.active_hold
            if hold and now >= hold.deadline_at:
                decision = self._latest_effective_decision(state, hold)
                if decision is None:
                    responsible = self._responsible_role(hold.deadline_key)
                    created += self._close_with_release(
                        state, hold.deadline_at, "hold_expired",
                        close_hold=("HOLD_EXPIRED",
                                    {"deadline_at": hold.deadline_at.isoformat(),
                                     "responsible_role": responsible},
                                    "期限届满无有效决定，默认放行"),
                        note="期限届满无有效决定，默认放行",
                    )
                elif decision["event_type"] in {"SUPERVISOR_REVIEWED", "APPEAL_DECIDED"}:
                    # 有效决定要求放行：机械执行，不依赖原经办人再点按钮
                    is_appeal = decision["event_type"] == "APPEAL_DECIDED"
                    created += self._close_with_release(
                        state, hold.deadline_at,
                        "appeal_upheld" if is_appeal else "supervisor_release",
                        close_hold=("HOLD_REVOKED",
                                    {"source": "appeal_upheld" if is_appeal else "supervisor_review",
                                     "revoked_by": "system", "decision_ref": decision["event_id"]},
                                    "按最新有效决定放行"),
                        note="按最新有效决定放行", decision_ref=decision["event_id"],
                    )
                # PAYMENT_STOPPED 有效时案件已进入 STOP_PAYMENT（上方跳过）；
                # continue/escalate 若无 HOLD_EXTENDED（新三要素）不产生延续效力，
                # 下一轮扫描仍按“无有效决定”届满放行。
        return created

    def _latest_effective_decision(self, state: CaseState, hold: HoldState) -> dict | None:
        """在 hold 生效后、截止前作出的、依据完整且权限匹配的最新决定。"""
        candidates = [
            e for e in state.events
            if e["event_type"] in REL_DECISION_EVENTS
            and hold.opened_at <= parse_dt(e["occurred_at"]) <= hold.deadline_at
        ]
        # 倒序找第一条能改变执行动作的有效决定
        for e in reversed(candidates):
            et, d = e["event_type"], e.get("data") or {}
            if et == "SUPERVISOR_REVIEWED" and d.get("decision") == "release":
                return e
            if et == "APPEAL_DECIDED" and d.get("upheld") is True:
                return e
            if et == "PAYMENT_STOPPED" and d.get("basis_type") in {"customer_request", "legal_order"}:
                return e
        return None

    @staticmethod
    def _responsible_role(deadline_key: str) -> str:
        return {
            "SUPERVISOR_45MIN": "supervisor",
            "EXTENSION_4H": "supervisor",
            "AUTHORITY_48H": "fraud_liaison",
        }.get(deadline_key, "supervisor")

    def _close_with_release(self, state: CaseState, at: datetime, release_reason: str,
                            close_hold: tuple, note: str, decision_ref: str | None = None) -> list[dict]:
        """闭合当前持有链并发 FUNDS_RELEASED；版本按同聚合既有最大值递增。"""
        hold = state.active_hold
        ts = at.isoformat()
        hold_type, hold_data, hold_summary = close_hold
        close_evt = self._evt(
            "protective_hold", hold.hold_id, hold_type, ts, hold_data, hold_summary,
        )
        close_evt["version"] = self._next_version(state.case_id, hold.hold_id)

        fd_id = f"fd-{state.case_id}"
        payload = {"release_reason": release_reason}
        if decision_ref:
            payload["decision_ref"] = decision_ref
        release_evt = self._evt("final_decision", fd_id, "FUNDS_RELEASED", ts, payload, note)
        release_evt["version"] = self._next_version(state.case_id, fd_id)

        out = []
        for ev in (close_evt, release_evt):
            ev["case_id"] = state.case_id
            if self.store.append(ev).status == "accepted":
                out.append(ev)
        return out

    def _next_version(self, case_id: str, aggregate_id: str) -> int:
        return 1 + max(
            (e["version"] for e in self.store.events_for_case(case_id) if e["aggregate_id"] == aggregate_id),
            default=0,
        )

    def _evt(self, agg_type, agg_id, etype, ts, data, summary, actor_role="system") -> dict:
        return {
            "event_id": self.id_gen(f"evt-{etype.lower()}"),
            "event_type": etype,
            "aggregate_type": agg_type,
            "aggregate_id": agg_id,
            "occurred_at": ts,
            "version": 1,
            "summary": summary,
            "case_id": "",  # 由 EventStore.append 回填
            "actor": {"role": actor_role},
            "data": data,
        }


# ---- 视图 -------------------------------------------------------------------

class TellerView:
    """柜员界面：倒计时 + 责任人 + 唯一的下一动作（§13）。"""

    def __init__(self, store: EventStore) -> None:
        self.proj = CaseProjector(store)

    def render(self, case_id: str, now: datetime) -> dict:
        s = self.proj.state(case_id, now)
        if s is None:
            return {"error": "案件不存在"}
        view = {"case_id": case_id, "status": s.status, "customer_id": s.customer_id}

        if s.status in {"CLOSED_RELEASED", "STOP_PAYMENT"}:
            view.update(next_action="案件已关闭，无待办", owner=None, deadline=None, seconds_left=None)
            return view

        if s.appeal and s.appeal["decided"] is None and s.active_hold:
            d = s.appeal["deadline"]
            view.update(
                deadline=d.isoformat(), seconds_left=max(0, int((d - now).total_seconds())),
                owner=s.appeal["reviewer"] or "independent_reviewer",
                next_action="独立复核人审查申诉（非原经办人），维持须附新的书面理由",
            )
            return view

        hold = s.active_hold
        if hold:
            d = hold.deadline_at
            view.update(
                deadline=d.isoformat(),
                seconds_left=max(0, int((d - now).total_seconds())),
                owner=hold.approver,
                next_action=self._held_next_action(s, hold),
                default_outcome="到点无有效决定：默认放行",
            )
            return view

        d15 = s.opened_at + timedelta(minutes=POLICY_MINUTES["INITIAL_VERIFICATION"])
        view.update(
            deadline=d15.isoformat(), seconds_left=max(0, int((d15 - now).total_seconds())),
            owner="teller",
            next_action="提供单独沟通机会、记录可观察信号、尝试联系；通过即放款，需延缓须主管批准三要素",
        )
        return view

    @staticmethod
    def _held_next_action(s: CaseState, hold: HoldState) -> str:
        if hold.deadline_key == "SUPERVISOR_45MIN":
            return "主管在截止前作出复核：放行 / 凭新三要素延续 / 升级"
        if hold.deadline_key == "EXTENSION_4H":
            return "完成联系尝试；无法排除风险且警方已受案的，凭受案记录申请 48h 段，否则放行"
        if hold.deadline_key == "AUTHORITY_48H":
            return "等待警方合法响应：L3 文书转正式流程；无合法延续依据则放行"
        return "作出复核决定"


class CustomerNotice:
    """客户书面理由与快速申诉入口（§9.1/§14）。"""

    TEMPLATE_HEAD = "这不是拒绝您的交易，也不是冻结或扣划您的资金，只是一次有时限的核实安排。"

    def __init__(self, store: EventStore) -> None:
        self.proj = CaseProjector(store)

    def render(self, case_id: str, now: datetime) -> dict | None:
        s = self.proj.state(case_id, now)
        if s is None or s.active_hold is None:
            return None
        hold = s.active_hold
        signal_by_id = {e["event_id"]: e for e in s.events if e["event_type"] == "SIGNAL_RECORDED"}
        contact_refs = [e for e in s.events if e["event_type"] == "CONTACT_ATTEMPTED"]
        contact_by_id = {e["event_id"]: e for e in contact_refs}

        facts_plain = []
        for ref in hold.reason_refs:
            ev = signal_by_id.get(ref) or contact_by_id.get(ref)
            if ev is None:
                facts_plain.append(f"（依据 {ref}）")
            elif ev["event_type"] == "SIGNAL_RECORDED":
                facts_plain.append(ev["data"]["detail"])
            else:
                result_map = {
                    "blocked_on_site": "现场有人阻止您接听电话",
                    "unreachable": "预留电话暂未接通",
                    "alert_hit": "反诈热线有与本笔交易相关的预警",
                    "conflicting_opinion": "紧急联系人的说法与您的表述不一致",
                }
                facts_plain.append(result_map.get(ev["data"]["result"], ev["data"]["result"]))

        appeal_code = next(
            (e["data"].get("appeal_code") for e in reversed(s.events)
             if e["event_type"] == "HOLD_APPLIED" and e["data"].get("appeal_code")),
            s.appeal["code"] if s.appeal else None,
        )
        return {
            "case_id": case_id,
            "headline": self.TEMPLATE_HEAD,
            "reasons_in_plain_language": facts_plain,
            "deadline_at": hold.deadline_at.isoformat(),
            "approver": hold.approver,
            "appeal_code": appeal_code,
            "appeal_channels": ["申诉码扫码", "专线电话", "现场要求换人"],
            "statement": "您本人有权随时表达意愿；届满若无新的有效决定，资金将按您的申请放行。",
            "delivered_at_required": True,
        }


class ExternalBusinessView:
    """跨业务外溢隔离（§15.3）：默认看不到任何本案内容。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    def for_customer(self, customer_id: str, now: datetime) -> dict:
        visible = []
        for case_id in self.store.cases():
            s = CaseProjector(self.store).state(case_id, now)
            if s is None or s.customer_id != customer_id:
                continue
            # 放行结案后立即为空；仅保留已受案/已法律定性的状态码
            if s.status == "CLOSED_RELEASED":
                continue
            if s.authority_level in {"L2", "L3"}:
                visible.append({"code": "AUTHORITY_CASE" if s.authority_level == "L2" else "LEGAL_ORDER",
                                "case_ref": case_id})
        return {"customer_id": customer_id, "visible": visible}  # visible 为空即“无任何外溢标记”


class PoliceDisclosureGate:
    """警方披露门（§15.4）：按授权等级决定可导出的材料范围。"""

    def __init__(self, store: EventStore) -> None:
        self.proj = CaseProjector(store)

    def request(self, case_id: str, level: str, case_number: str | None = None,
                scope: str | None = None, now: datetime | None = None) -> dict:
        now = now or datetime.now().astimezone()
        if level not in {"L0", "L1", "L2", "L3"}:
            return {"allowed": False, "reason": "授权等级无效"}
        if level in {"L2", "L3"} and not case_number:
            return {"allowed": False, "reason": f"{level} 请求必须提供受案/文书编号"}

        s = self.proj.state(case_id, now)
        if s is None:
            return {"allowed": False, "reason": "案件不存在"}

        if level in {"L0", "L1"}:
            return {"allowed": False, "deliverable": "仅可口头告知‘本行正按流程核实’，不导出任何客户材料",
                    "logged": True}

        package = self._package(s, level, case_number, scope)
        return {"allowed": True, "level": level, "case_number": case_number, **package}

    @staticmethod
    def _package(s: CaseState, level, case_number, scope):
        events = s.events
        if level == "L2":
            # 受案/协查：只出与本案直接相关的事实信号与联系摘要；印象记录一律不出
            signals = [{"ref": e["event_id"], "code": e["data"]["code"], "detail": e["data"]["detail"]}
                       for e in events if e["event_type"] == "SIGNAL_RECORDED" and e["data"]["grade"] == "fact"]
            contacts = [{"target": e["data"]["target"], "result": e["data"]["result"]}
                        for e in events if e["event_type"] == "CONTACT_ATTEMPTED"]
            return {"fact_signals": signals, "contact_summary": contacts,
                    "note": "L2 仅提供有限材料；impression 记录、账户流水与资金去向不在范围内"}
        # L3：按文书范围提供完整证据；impression 仍显式标注为“主观印象”，不得当事实使用
        signals = [{"ref": e["event_id"], "grade": e["data"]["grade"], "code": e["data"]["code"],
                    "detail": e["data"]["detail"]}
                   for e in events if e["event_type"] == "SIGNAL_RECORDED"]
        return {"scope_approved": scope, "all_events": events, "signals": signals,
                "funds_tracing": "按文书范围提供", "note": "完整证据与资金去向仅限文书载明范围"}


# ---- 命令侧：把业务规则（不只是结构校验）挡在入口 ----------------------------

# 可单独/组合支撑保护措施的强依据；其余（印象、家属反对、单纯未接通）只能触发核实
_STRONG_CONTACT_RESULTS = {"blocked_on_site", "alert_hit"}
_STRONG_SIGNAL_GRADES = {"fact", "system_notice"}
_HOLD_APPROVER_ROLES = {"supervisor", "backup_supervisor", "branch_manager"}


class CaseCommands:
    """案件写操作。所有写入仍走 EventStore，保证幂等与时间轴规则统一。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    # -- 信号/联系/陈述（柜员现场记录） --
    def record_signal(self, case_id, ts, grade, code, detail, source="teller",
                      actor=("teller", None, None)) -> AppendResult:
        return self._append(case_id, self._new_id(case_id, "sig"), "observed_signal",
                            "SIGNAL_RECORDED", ts,
                            {"grade": grade, "code": code, "detail": detail, "source": source},
                            f"记录信号：{detail}", actor)

    def record_contact(self, case_id, ts, target, method, result, note="",
                       actor=("teller", None, None)) -> AppendResult:
        return self._append(case_id, case_id, "withdrawal_case", "CONTACT_ATTEMPTED", ts,
                            {"target": target, "method": method, "result": result, "note": note},
                            f"联系尝试：{target}/{result}", actor)

    def customer_statement(self, case_id, ts, words, consistent, separate=True,
                           actor=("teller", None, None)) -> AppendResult:
        return self._append(case_id, case_id, "withdrawal_case", "CUSTOMER_STATEMENT", ts,
                            {"separate_setting": separate, "in_own_words": words,
                             "consistent": consistent},
                            f"客户单独表达：{words[:40]}", actor)

    # -- 保护措施（三要素 + 强依据，§9） --
    def apply_hold(self, case_id, ts, reason_refs, deadline_key, approver,
                   customer_notified_at, appeal_code=None, accelerated=False,
                   actor=("supervisor", None, None)) -> AppendResult:
        prior = self.store.events_for_case(case_id)
        if not prior:
            return AppendResult("invalid", "案件不存在或尚未开案")
        if actor[0] not in _HOLD_APPROVER_ROLES:
            return AppendResult("invalid", "保护措施必须由主管级角色批准")
        ok, why = self._hold_basis_sufficient(prior, reason_refs)
        if not ok:
            return AppendResult("invalid", why)

        flagged = next(e for e in prior if e["event_type"] == "TRANSACTION_FLAGGED")
        t0 = parse_dt(flagged["occurred_at"])
        when = parse_dt(ts)
        deadline = deadline_for(t0, deadline_key, accelerated)
        if when > deadline:
            return AppendResult("invalid", f"已超过 {deadline_key} 的截止时刻，不能再施加该段措施")

        data = {"reason_signal_refs": reason_refs, "deadline_key": deadline_key,
                "deadline_at": deadline.isoformat(), "approver": approver,
                "customer_notified_at": customer_notified_at}
        if appeal_code:
            data["appeal_code"] = appeal_code
        if accelerated:
            data["accelerated"] = True
        return self._append(case_id, self._new_id(case_id, "hold"), "protective_hold",
                            "HOLD_APPLIED", ts, data, "施加保护措施（流程性暂停）", actor)

    @staticmethod
    def _hold_basis_sufficient(prior_events, refs) -> tuple[bool, str]:
        """年龄/金额/家属反对/印象只能触发核实，不能单独支撑暂停（§7.2/§7.3）。"""
        if not refs:
            return False, "保护措施必须引用至少一条具体事实依据"
        by_id = {e["event_id"]: e for e in prior_events}
        missing = [r for r in refs if r not in by_id]
        if missing:
            return False, f"引用的依据不存在：{', '.join(missing)}"
        for r in refs:
            e = by_id[r]
            if e["event_type"] == "SIGNAL_RECORDED" and e["data"]["grade"] in _STRONG_SIGNAL_GRADES:
                return True, ""
            if e["event_type"] == "CONTACT_ATTEMPTED" and e["data"]["result"] in _STRONG_CONTACT_RESULTS:
                return True, ""
        return False, ("家属反对、单纯未接通、主观印象均只能触发核实，不能单独支撑保护措施；"
                       "需至少一条 fact/system_notice 信号，或‘被阻止接电话/反诈预警命中’联系结果")

    def extend_hold(self, case_id, ts, prior_hold_ref, reason_refs, deadline_key, approver,
                    authority_level=None, actor=("supervisor", None, None)) -> AppendResult:
        if deadline_key == "AUTHORITY_48H" and authority_level not in {"L2", "L3"}:
            return AppendResult("invalid", "48 小时警方窗口须有 L2 受案或 L3 文书")
        prior = self.store.events_for_case(case_id)
        prior_hold = next((e for e in prior if e["aggregate_id"] == prior_hold_ref
                           and e["event_type"] in {"HOLD_APPLIED", "HOLD_EXTENDED"}), None)
        if prior_hold is None:  # 允许引用命令返回的事件 ID
            prior_hold = next((e for e in prior if e["event_id"] == prior_hold_ref
                               and e["event_type"] in {"HOLD_APPLIED", "HOLD_EXTENDED"}), None)
        if prior_hold is None:
            return AppendResult("invalid", f"找不到前序措施 {prior_hold_ref}")
        # §8：延续段从上一段截止起算；48h 警方窗口从受案/预警记录到达起算
        anchor = parse_dt(prior_hold["data"]["deadline_at"])
        if deadline_key == "AUTHORITY_48H":
            l2 = next((e for e in reversed(prior) if e["event_type"] == "AUTHORITY_RESPONDED"
                       and e["data"]["authority_level"] in {"L2", "L3"}), None)
            if l2 is None:
                return AppendResult("invalid", "48 小时警方窗口缺少 L2/L3 响应事件")
            anchor = parse_dt(l2["occurred_at"])
        multiplier = 0.5 if prior_hold["data"].get("accelerated") and deadline_key in ACCELERATED_KEYS else 1.0
        deadline = anchor + timedelta(seconds=round(POLICY_MINUTES[deadline_key] * 60 * multiplier))
        data = {"prior_hold_ref": prior_hold_ref, "reason_signal_refs": reason_refs,
                "deadline_key": deadline_key, "deadline_at": deadline.isoformat(),
                "approver": approver}
        if authority_level:
            data["authority_level"] = authority_level
        if multiplier == 0.5:
            data["accelerated"] = True
        return self._append(case_id, self._new_id(case_id, "hold"), "protective_hold",
                            "HOLD_EXTENDED", ts, data, "延续保护措施（新三要素）", actor)

    # -- 申诉 --
    def file_appeal(self, case_id, ts, appeal_code, channel="in_person",
                    filed_by="customer", actor=("customer", None, None)) -> AppendResult:
        return self._append(case_id, case_id, "withdrawal_case", "APPEAL_FILED", ts,
                            {"appeal_code": appeal_code, "channel": channel, "filed_by": filed_by},
                            "客户提出快速申诉", actor)

    def decide_appeal(self, case_id, ts, appeal_code, upheld, reviewer, written_reason,
                      actor=("independent_reviewer", None, None)) -> AppendResult:
        if actor[0] != "independent_reviewer":
            return AppendResult("invalid", "申诉必须由非原经办的独立复核人审查")
        data = {"appeal_code": appeal_code, "upheld": upheld, "reviewer": reviewer,
                "written_reason": written_reason}
        return self._append(case_id, f"fd-{case_id}", "final_decision", "APPEAL_DECIDED", ts,
                            data, "申诉审查结果", actor)

    # -- 放行/止付 --
    def release(self, case_id, ts, reason, decision_ref=None,
                actor=("supervisor", None, None)) -> AppendResult:
        data = {"release_reason": reason}
        if decision_ref:
            data["decision_ref"] = decision_ref
        return self._append(case_id, f"fd-{case_id}", "final_decision", "FUNDS_RELEASED", ts,
                            data, "放行交易", actor)

    def stop_payment(self, case_id, ts, basis_type, approver, customer_notified_at,
                     customer_request_ref=None, legal_case_number=None,
                     actor=("branch_manager", None, None)) -> AppendResult:
        if basis_type == "customer_request" and not customer_request_ref:
            return AppendResult("invalid", "客户请求止付必须附书面/录音凭证")
        if basis_type == "legal_order" and not legal_case_number:
            return AppendResult("invalid", "法律文书止付必须填写文书编号")
        data = {"basis_type": basis_type, "approver": approver,
                "customer_notified_at": customer_notified_at}
        if customer_request_ref:
            data["customer_request_ref"] = customer_request_ref
        if legal_case_number:
            data["legal_case_number"] = legal_case_number
        return self._append(case_id, f"fd-{case_id}", "final_decision", "PAYMENT_STOPPED", ts,
                            data, "止付（有客户请求或法律依据）", actor)

    # -- 加速 --
    def grant_acceleration(self, case_id, ts, approver, granted=True,
                           actor=("supervisor", None, None)) -> AppendResult:
        return self._append(case_id, case_id, "withdrawal_case", "ACCELERATION_GRANTED", ts,
                            {"granted": granted, "approver": approver, "deadline_multiplier": 0.5},
                            "真实交易加速：时限减半", actor)

    # -- 内部 --
    def _new_id(self, case_id, prefix) -> str:
        n = 1 + sum(1 for e in self.store.events_for_case(case_id)
                    if e["aggregate_id"].startswith(f"{prefix}-{case_id}"))
        return f"{prefix}-{case_id}-{n:02d}"

    def _next_version(self, case_id, agg_id) -> int:
        return 1 + max((e["version"] for e in self.store.events_for_case(case_id)
                        if e["aggregate_id"] == agg_id), default=0)

    def _append(self, case_id, agg_id, agg_type, etype, ts, data, summary, actor) -> AppendResult:
        role, staff_id, name = actor
        event = {
            "event_id": f"evt-{case_id}-{etype.lower()}-{self._next_version(case_id, agg_id)}-{agg_id[-4:]}",
            "event_type": etype,
            "aggregate_type": agg_type,
            "aggregate_id": agg_id,
            "occurred_at": ts if isinstance(ts, str) else ts.isoformat(),
            "version": self._next_version(case_id, agg_id),
            "summary": summary,
            "case_id": case_id,
            "actor": {"role": role, **({"staff_id": staff_id} if staff_id else {}),
                      **({"name": name} if name else {})},
            "data": data,
        }
        return self.store.append(event)


# ---- 时限计算辅助（供命令侧构造 HOLD_APPLIED/HOLD_EXTENDED） -----------------

def deadline_for(t0: datetime, deadline_key: str, accelerated: bool = False) -> datetime:
    multiplier = 0.5 if accelerated and deadline_key in ACCELERATED_KEYS else 1.0
    return t0 + timedelta(seconds=round(POLICY_MINUTES[deadline_key] * 60 * multiplier))
