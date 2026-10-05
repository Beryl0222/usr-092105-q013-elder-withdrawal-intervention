import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.intervention import InterventionService, LegalAuthorization, Role

TZ = timezone(timedelta(hours=8))
CASE = "case-test-001"


def env(event_id, event_type, aggregate_type, aggregate_id, version, occurred_at, summary, payload):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "summary": summary,
        "payload": payload,
    }


def flag(event_id="evt-f-1", case_id=CASE, occurred="2026-09-21T09:00:00+08:00", version=1, trigger_kind="amount"):
    return env(event_id, "TRANSACTION_FLAGGED", "withdrawal_case", case_id, version, occurred, "触发核实", {
        "case_id": case_id,
        "customer_id": "cust-1",
        "amount": 480000,
        "currency": "CNY",
        "declared_purpose": "购房首付",
        "destination": "某房企资金监管账户 6228****9012",
        "channel": "柜面",
        "teller_id": "teller-07",
        "trigger": {"kind": trigger_kind, "detail": "达到大额关注标准"},
    })


def self_expr(event_id, case_id, version, occurred, independent=True):
    return env(event_id, "SELF_EXPRESSION_RECORDED", "withdrawal_case", case_id, version, occurred, "客户自主表达", {
        "case_id": case_id,
        "statement": "客户本人陈述：取款用于购房首付，系自愿",
        "expressed_independently": independent,
        "companion_present": not independent,
        "recorded_by": "teller-07",
    })


def review(event_id, case_id, version, occurred, conclusion="concern_confirmed", reviewer="sup-01"):
    return env(event_id, "SUPERVISOR_REVIEWED", "withdrawal_case", case_id, version, occurred, "主管复核", {
        "case_id": case_id,
        "reviewer_id": reviewer,
        "reviewer_role": Role.SUPERVISOR,
        "conclusion": conclusion,
        "rationale": "多项现场信号且联系受阻",
    })


def hold(event_id, case_id, hold_id, occurred, minutes=60, version=1):
    return env(event_id, "HOLD_APPLIED", "protective_hold", hold_id, version, occurred, "保护性止付", {
        "case_id": case_id,
        "reason": "客户意愿无法独立确认，需保护性核实",
        "duration_minutes": minutes,
        "escalation_owner_role": Role.SUPERVISOR,
        "decider_id": "sup-01",
        "decider_role": Role.SUPERVISOR,
    })


def decision(event_id, case_id, decision_id, version, occurred, outcome, decider="sup-01", role=Role.SUPERVISOR):
    return env(event_id, "DECISION_RECORDED", "final_decision", decision_id, version, occurred, "最终决定", {
        "case_id": case_id,
        "outcome": outcome,
        "basis": "主管复核结论及已确认事实",
        "decider_id": decider,
        "decider_role": role,
    })


def signal(event_id, case_id, signal_id, occurred, assessment=None):
    payload = {"case_id": case_id, "kind": "blocks_contact", "observation": "客户手机来电被陪同者按断"}
    if assessment:
        payload["assessment"] = assessment
    return env(event_id, "SIGNAL_RECORDED", "observed_signal", signal_id, 1, occurred, "现场信号", payload)


def appeal(event_id, case_id, version, occurred):
    return env(event_id, "APPEAL_FILED", "withdrawal_case", case_id, version, occurred, "客户申诉", {
        "case_id": case_id,
        "filed_by": "客户本人",
        "channel": "网点现场",
        "grounds": "真实购房，请求放行",
    })


def appeal_review(event_id, case_id, version, occurred, outcome, reviewer="appeals-01", role=Role.APPEALS_OFFICER):
    return env(event_id, "APPEAL_REVIEWED", "withdrawal_case", case_id, version, occurred, "申诉复核", {
        "case_id": case_id,
        "reviewer_id": reviewer,
        "reviewer_role": role,
        "outcome": outcome,
        "rationale": "客户独立陈述与佐证材料支持放行",
    })


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 21, 9, 0, tzinfo=TZ)
        self.svc = InterventionService(clock=lambda: self.now)

    def _open_case_with_hold(self):
        self.svc.ingest(flag())
        self.svc.ingest(self_expr("evt-se-1", CASE, 2, "2026-09-21T09:05:00+08:00"))
        self.svc.ingest(review("evt-rv-1", CASE, 3, "2026-09-21T09:10:00+08:00"))
        return self.svc.ingest(hold("evt-h-1", CASE, "hold-1", "2026-09-21T09:15:00+08:00"))

    # ---------- 触发语义 ----------

    def test_trigger_only_starts_verification(self):
        for kind in ("age", "amount", "family_objection"):
            svc = InterventionService(clock=lambda: self.now)
            result = svc.ingest(flag(trigger_kind=kind))
            self.assertEqual(result.status, "accepted")
            view = svc.case_view(CASE)
            self.assertEqual(view["status"], "核实中")
            self.assertEqual(view["transaction_capability"], "未受影响")
            self.assertEqual(view["next_action"]["owner_role"], Role.TELLER)

    # ---------- 幂等与时间语义 ----------

    def test_ingest_is_idempotent(self):
        first = self.svc.ingest(flag())
        second = self.svc.ingest(flag())
        self.assertEqual(first.status, "accepted")
        self.assertEqual(second.status, "duplicate")
        sig = signal("evt-s-1", CASE, "sig-1", "2026-09-21T09:08:00+08:00", assessment="疑似受他人操控")
        self.svc.ingest(sig)
        self.assertEqual(self.svc.ingest(sig).status, "duplicate")
        view = self.svc.case_view(CASE)
        self.assertEqual(view["counts"]["signals"], 1)

    def test_duplicate_with_different_content_rejected(self):
        self.svc.ingest(flag())
        tampered = flag()
        tampered["summary"] = "被篡改的摘要"
        result = self.svc.ingest(tampered)
        self.assertEqual(result.status, "rejected")
        self.assertTrue(any("冲突" in e for e in result.errors))

    def test_version_conflict_rejected(self):
        self.svc.ingest(flag())
        result = self.svc.ingest(flag(event_id="evt-f-2"))
        self.assertEqual(result.status, "rejected")
        self.assertTrue(any("版本冲突" in e for e in result.errors))

    def test_envelope_missing_fields_rejected(self):
        bad = flag()
        del bad["version"]
        result = self.svc.ingest(bad)
        self.assertEqual(result.status, "rejected")
        self.assertIn("缺少字段：version", result.errors)

    def test_occurred_at_drives_deadlines_not_arrival_time(self):
        self.svc.ingest(flag())
        self.svc.ingest(self_expr("evt-se-1", CASE, 2, "2026-09-21T09:05:00+08:00"))
        self.svc.ingest(review("evt-rv-1", CASE, 3, "2026-09-21T09:10:00+08:00"))
        late_arrival = datetime(2026, 9, 21, 12, 0, tzinfo=TZ)
        result = self.svc.ingest(
            hold("evt-h-1", CASE, "hold-1", "2026-09-21T09:15:00+08:00", minutes=60),
            received_at=late_arrival,
        )
        self.assertEqual(result.status, "accepted")
        self.now = datetime(2026, 9, 21, 9, 30, tzinfo=TZ)
        view = self.svc.case_view(CASE, now=self.now)
        # 期限按事实发生时间 09:15 起算，不因 12:00 才送达而推迟
        self.assertEqual(view["current_hold"]["applied_at"], "2026-09-21T09:15:00+08:00")
        self.assertEqual(view["current_hold"]["expires_at"], "2026-09-21T10:15:00+08:00")
        item = view["next_action"]
        self.assertEqual(item["countdown_seconds"], 45 * 60)
        self.assertEqual(item["owner_role"], Role.SUPERVISOR)

    # ---------- 保护性措施三要素与届满规则 ----------

    def test_hold_requires_reason_duration_owner(self):
        self.svc.ingest(flag())
        self.svc.ingest(self_expr("evt-se-1", CASE, 2, "2026-09-21T09:05:00+08:00"))
        self.svc.ingest(review("evt-rv-1", CASE, 3, "2026-09-21T09:10:00+08:00"))
        bad = hold("evt-h-1", CASE, "hold-1", "2026-09-21T09:15:00+08:00")
        del bad["payload"]["reason"]
        del bad["payload"]["escalation_owner_role"]
        bad["payload"]["duration_minutes"] = 0
        result = self.svc.ingest(bad)
        self.assertEqual(result.status, "rejected")
        self.assertIn("payload 缺少字段：reason", result.errors)
        self.assertIn("payload 缺少字段：escalation_owner_role", result.errors)
        self.assertTrue(any("duration_minutes" in e for e in result.errors))

    def test_hold_requires_supervisor_review_first(self):
        self.svc.ingest(flag())
        result = self.svc.ingest(hold("evt-h-1", CASE, "hold-1", "2026-09-21T09:15:00+08:00"))
        self.assertEqual(result.status, "rejected")
        self.assertTrue(any("主管复核" in e for e in result.errors))

    def test_decline_requires_confirmed_basis(self):
        self.svc.ingest(flag(trigger_kind="family_objection"))
        result = self.svc.ingest(decision("evt-d-1", CASE, "dec-1", 1, "2026-09-21T09:30:00+08:00", "decline"))
        self.assertEqual(result.status, "rejected")
        self.assertTrue(any("不足以否定客户交易能力" in e for e in result.errors))
        self.svc.ingest(review("evt-rv-1", CASE, 2, "2026-09-21T09:10:00+08:00"))
        result = self.svc.ingest(decision("evt-d-1", CASE, "dec-1", 1, "2026-09-21T09:30:00+08:00", "decline"))
        self.assertEqual(result.status, "accepted")

    def test_hold_expiry_defaults_to_release_and_flags_breach(self):
        self.assertEqual(self._open_case_with_hold().status, "accepted")
        self.now = datetime(2026, 9, 21, 10, 20, tzinfo=TZ)  # 措施 10:15 已届满
        view = self.svc.case_view(CASE, now=self.now)
        self.assertEqual(view["effective_outcome"], "release")
        self.assertEqual(view["outcome_basis"], "hold_expired_default")
        self.assertTrue(view["escalation_breach"])
        self.assertEqual(view["transaction_capability"], "未受影响")
        item = view["next_action"]
        self.assertEqual(item["owner_role"], Role.SUPERVISOR)
        self.assertTrue(item["breach"])
        self.assertIn("默认放行", item["action"])

    def test_latest_valid_decision_governs_at_expiry(self):
        self._open_case_with_hold()
        # 届满前登记止付决定（已有主管复核确认风险）
        result = self.svc.ingest(decision("evt-d-1", CASE, "dec-1", 1, "2026-09-21T10:10:00+08:00", "decline"))
        self.assertEqual(result.status, "accepted")
        self.now = datetime(2026, 9, 21, 10, 20, tzinfo=TZ)
        view = self.svc.case_view(CASE, now=self.now)
        self.assertEqual(view["effective_outcome"], "decline")
        self.assertFalse(view["escalation_breach"])
        # 申诉推翻 → 原决定失效 → 新的放行决定成为最新有效决定
        self.svc.ingest(appeal("evt-a-1", CASE, 4, "2026-09-21T10:25:00+08:00"))
        self.svc.ingest(appeal_review("evt-a-2", CASE, 5, "2026-09-21T10:40:00+08:00", "overturn"))
        result = self.svc.ingest(
            decision("evt-d-2", CASE, "dec-1", 2, "2026-09-21T10:45:00+08:00", "release",
                     decider="appeals-01", role=Role.APPEALS_OFFICER)
        )
        self.assertEqual(result.status, "accepted")
        self.now = datetime(2026, 9, 21, 10, 50, tzinfo=TZ)
        view = self.svc.case_view(CASE, now=self.now)
        self.assertEqual(view["effective_outcome"], "release")
        self.assertEqual(view["transaction_capability"], "未受影响")

    def test_extension_after_expiry_rejected(self):
        self._open_case_with_hold()
        late = env("evt-hx-1", "HOLD_EXTENDED", "protective_hold", "hold-1", 2,
                   "2026-09-21T10:16:00+08:00", "延期", {
                       "case_id": CASE, "reason": "仍需核实", "duration_minutes": 60,
                       "escalation_owner_role": Role.SUPERVISOR, "approved_by": "compliance-1",
                   })
        result = self.svc.ingest(late)
        self.assertEqual(result.status, "rejected")
        self.assertTrue(any("已届满" in e for e in result.errors))
        early = env("evt-hx-2", "HOLD_EXTENDED", "protective_hold", "hold-1", 2,
                    "2026-09-21T10:00:00+08:00", "延期", {
                        "case_id": CASE, "reason": "仍需核实", "duration_minutes": 60,
                        "escalation_owner_role": Role.SUPERVISOR, "approved_by": "compliance-1",
                    })
        self.assertEqual(self.svc.ingest(early).status, "accepted")
        view = self.svc.case_view(CASE, now=datetime(2026, 9, 21, 10, 5, tzinfo=TZ))
        self.assertEqual(view["current_hold"]["expires_at"], "2026-09-21T11:00:00+08:00")

    # ---------- 柜员界面：倒计时与责任人 ----------

    def test_worklist_shows_owner_and_countdown(self):
        self.svc.ingest(flag())
        items = self.svc.teller_worklist(now=self.now)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.owner_role, Role.TELLER)
        self.assertIn("独立陈述", item.action)
        self.assertEqual(item.countdown_seconds(self.now), 30 * 60)
        self.assertFalse(item.is_overdue(self.now))
        later = datetime(2026, 9, 21, 9, 31, tzinfo=TZ)
        self.assertTrue(item.is_overdue(later))
        self.assertEqual(item.countdown_seconds(later), 0)

    # ---------- 客户书面理由与申诉 ----------

    def test_customer_notice_contains_reason_and_appeal_entry(self):
        self.svc.ingest(flag())
        self.svc.ingest(self_expr("evt-se-1", CASE, 2, "2026-09-21T09:05:00+08:00", independent=False))
        self.svc.ingest(signal("evt-s-1", CASE, "sig-1", "2026-09-21T09:08:00+08:00", assessment="疑似受他人操控"))
        self.svc.ingest(review("evt-rv-1", CASE, 3, "2026-09-21T09:10:00+08:00"))
        self.svc.ingest(hold("evt-h-1", CASE, "hold-1", "2026-09-21T09:15:00+08:00"))
        notice = self.svc.customer_notice(CASE)
        text = json.dumps(notice, ensure_ascii=False)
        self.assertIn("不代表对您交易能力的否定", text)
        self.assertIn("客户意愿无法独立确认", text)  # 书面理由
        self.assertIn("申诉", notice["appeal_channel"])
        self.assertIsNotNone(notice["appeal_deadline"])
        self.assertEqual(notice["measure_expires_at"], "2026-09-21T10:15:00+08:00")
        self.assertNotIn("疑似受他人操控", text)  # 未确认主观标签不进书面理由

    def test_appeal_flow_and_reviewer_must_differ(self):
        self._open_case_with_hold()
        self.svc.ingest(decision("evt-d-1", CASE, "dec-1", 1, "2026-09-21T10:10:00+08:00", "decline"))
        self.svc.ingest(appeal("evt-a-1", CASE, 4, "2026-09-21T10:25:00+08:00"))
        # 申诉待复核期间，柜员界面呈现申诉复核动作与责任人
        items = self.svc.teller_worklist(now=datetime(2026, 9, 21, 10, 30, tzinfo=TZ))
        self.assertEqual(items[0].owner_role, Role.APPEALS_OFFICER)
        same = self.svc.ingest(
            appeal_review("evt-a-2", CASE, 5, "2026-09-21T10:40:00+08:00", "overturn", reviewer="sup-01")
        )
        self.assertEqual(same.status, "rejected")
        self.assertTrue(any("原决定人" in e for e in same.errors))
        ok = self.svc.ingest(
            appeal_review("evt-a-3", CASE, 5, "2026-09-21T10:40:00+08:00", "overturn", reviewer="appeals-01")
        )
        self.assertEqual(ok.status, "accepted")

    # ---------- 主观标签隔离 ----------

    def test_unconfirmed_labels_stay_out_of_shared_view(self):
        self.svc.ingest(flag())
        self.svc.ingest(signal("evt-s-1", CASE, "sig-1", "2026-09-21T09:08:00+08:00", assessment="疑似受他人操控"))
        view = self.svc.shared_risk_view(CASE)
        text = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("疑似受他人操控", text)
        self.assertEqual(view["confirmed_observations"], [])
        self.assertEqual(view["transaction_capability"], "未受影响")

    def test_confirmed_signal_enters_shared_view(self):
        self.svc.ingest(flag())
        self.svc.ingest(signal("evt-s-1", CASE, "sig-1", "2026-09-21T09:08:00+08:00", assessment="疑似受他人操控"))
        self.svc.ingest(env("evt-s-2", "SIGNAL_CONFIRMED", "observed_signal", "sig-1", 2,
                            "2026-09-21T09:20:00+08:00", "信号确认",
                            {"case_id": CASE, "basis": "客户女儿到场证实"}))
        view = self.svc.shared_risk_view(CASE)
        self.assertEqual(view["confirmed_observations"], ["客户手机来电被陪同者按断"])
        self.assertEqual(view["confirmed_assessments"], ["疑似受他人操控"])

    # ---------- 警方分级披露 ----------

    def test_police_disclosure_requires_valid_authorization(self):
        self.svc.ingest(flag())
        minimal = self.svc.police_disclosure(CASE, None, now=self.now)
        self.assertEqual(minimal["level"], "minimal")
        self.assertNotIn("fund_destination", minimal)
        expired = LegalAuthorization(
            "警函-001", "某派出所", (CASE,),
            datetime(2026, 9, 1, tzinfo=TZ), datetime(2026, 9, 10, tzinfo=TZ),
            frozenset({"evidence", "fund_flow"}),
        )
        denied = self.svc.police_disclosure(CASE, expired, now=self.now)
        self.assertEqual(denied["level"], "denied")
        self.assertTrue(any("有效期" in e for e in denied["errors"]))
        wrong_scope = LegalAuthorization(
            "警函-002", "某派出所", ("other-case",),
            datetime(2026, 9, 1, tzinfo=TZ), datetime(2026, 10, 1, tzinfo=TZ),
            frozenset({"evidence"}),
        )
        denied2 = self.svc.police_disclosure(CASE, wrong_scope, now=self.now)
        self.assertEqual(denied2["level"], "denied")
        self.assertEqual(len(self.svc.disclosure_log()), 3)

    def test_police_full_disclosure_with_authorization(self):
        self.svc.ingest(flag())
        self.svc.ingest(signal("evt-s-1", CASE, "sig-1", "2026-09-21T09:08:00+08:00", assessment="疑似受他人操控"))
        auth = LegalAuthorization(
            "警函-003", "某公安分局", (CASE,),
            datetime(2026, 9, 1, tzinfo=TZ), datetime(2026, 10, 1, tzinfo=TZ),
            frozenset({"evidence", "fund_flow"}),
        )
        full = self.svc.police_disclosure(CASE, auth, now=self.now)
        self.assertEqual(full["level"], "full")
        self.assertEqual(full["fund_destination"], "某房企资金监管账户 6228****9012")
        self.assertEqual(full["timeline"][0]["occurred_at"], "2026-09-21T09:00:00+08:00")
        self.assertIn("疑似受他人操控", json.dumps(full["evidence"], ensure_ascii=False))

    # ---------- 支付执行约束 ----------

    def test_funds_release_requires_release_outcome(self):
        self._open_case_with_hold()
        self.svc.ingest(decision("evt-d-1", CASE, "dec-1", 1, "2026-09-21T10:10:00+08:00", "decline"))
        result = self.svc.ingest(env("evt-fr-1", "FUNDS_RELEASED", "final_decision", "dec-1", 2,
                                     "2026-09-21T10:12:00+08:00", "放款",
                                     {"case_id": CASE, "payment_ref": "pay-1"}))
        self.assertEqual(result.status, "rejected")
        self.assertTrue(any("不得放款" in e for e in result.errors))

    # ---------- 样例事件流 ----------

    def test_sample_case_file_flows_to_release(self):
        stream = json.loads((Path(__file__).parents[1] / "data" / "sample_case.json").read_text(encoding="utf-8"))
        results = self.svc.ingest_all(stream)
        self.assertTrue(all(r.ok for r in results), [r.errors for r in results])
        # 来源系统整体重发一遍，全部幂等去重
        again = self.svc.ingest_all(stream)
        self.assertTrue(all(r.status == "duplicate" for r in again))
        view = self.svc.case_view("case-20260921-001", now=datetime(2026, 9, 21, 12, 0, tzinfo=TZ))
        self.assertEqual(view["status"], "已结案（已放行）")
        self.assertEqual(view["transaction_capability"], "未受影响")
        self.assertFalse(view["escalation_breach"])
        self.assertIsNone(view["next_action"])
        shared = json.dumps(self.svc.shared_risk_view("case-20260921-001"), ensure_ascii=False)
        self.assertNotIn("疑似受他人操控", shared)


if __name__ == "__main__":
    unittest.main()
