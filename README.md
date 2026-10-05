# 涉老取款紧急干预

本项目保存“涉老取款紧急干预”领域的公共约定，并提供一套**有时限、可申诉**的紧急干预服务：网点与支付系统以事件形式反复通知，服务幂等接收并围绕案件给出下一动作、书面理由与分级披露视图。

## 领域资料

- `contracts/domain.schema.json`：事件信封与本领域允许的聚合、事件类型。
- `data/sample.json`：单条信封样例。
- `data/sample_case.json`：完整案件事件流（陪同者代答、催促、阻断联系 → 保护性止付 → 客户独立陈述确认为真实购房 → 解除并放行）。
- `src/validator.py`：信封公共字段校验。
- `src/intervention/`：紧急干预服务（见下）。
- `tests/`：契约测试与服务行为测试。

事件由 `event_id` 唯一标识，`aggregate_id` 指向业务对象，`version` 从 1 开始递增，`occurred_at` 保留真实发生时间。来源系统重试时必须沿用原事件标识。

## 设计原则

- **触发不等于否定**：年龄、金额或家属反对只启动核实，任何事件流都不能仅凭触发原因登记止付；止付决定必须基于已确认事实（主管复核确认风险或警方确认诈骗）。
- **保护措施三要素**：保护性止付必须携带理由、期限与升级责任人，且须先完成主管复核；延期只能在届满前办理，届满后须重新申请。
- **届满按最新有效决定执行**：措施届满时按最新有效决定放行或止付；没有有效决定时措施自动失效、**默认放行**，并登记升级责任失守。被申诉推翻的决定自动失效。
- **幂等与时间语义**：按 `event_id` 去重（同内容重发返回 `duplicate`，不同内容视为冲突拒收）；所有业务时限以 `occurred_at` 起算，接收时间单独记录，迟到或重试不会推迟任何期限。
- **主观标签隔离**：现场信号区分“可观察事实”与“主观标签”；未经确认的标签只留在卷宗内，不进入客户书面理由，也不进入其他银行业务视图。
- **分级披露**：警方无授权时仅可确认案件存在与否及联络渠道；持合法授权（文号、机关、有效期、范围）方可调取完整证据与资金去向，全部调取留痕。

## 案件构成

客户自主表达、陪同关系、交易上下文（金额、用途、资金去向）、现场可观察信号、联系尝试、主管复核、警方响应、最终决定（放行或止付），分别由 `withdrawal_case`、`observed_signal`、`protective_hold`、`final_decision` 四类聚合承载，事件类型见 `src/intervention/events.py`。

## 使用示例

```python
import json
from src.intervention import InterventionService, LegalAuthorization

svc = InterventionService()
stream = json.load(open("data/sample_case.json", encoding="utf-8"))
results = svc.ingest_all(stream)          # 幂等接收，重发返回 duplicate

# 柜员界面：下一动作 + 责任人 + 倒计时
for item in svc.teller_worklist():
    print(item.as_dict(svc.clock()))

# 客户书面理由与快速申诉入口
notice = svc.customer_notice("case-20260921-001")

# 其他银行业务视图（未确认主观标签已被隔离）
shared = svc.shared_risk_view("case-20260921-001")

# 警方披露：无授权仅最低限度信息；持合法授权方可调取完整证据与资金去向
minimal = svc.police_disclosure("case-20260921-001")
```

## 本地检查

运行 `python3 -m unittest discover -s tests`。
