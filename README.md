# 涉老取款紧急干预

本项目保存“涉老取款紧急干预”领域中跨机构交换记录的基础约定。当前代码提供事件信封校验、中文示例数据和最小测试入口，便于后续服务在不改变公共标识语义的前提下扩展业务。

## 领域资料

- `contracts/domain.schema.json`：事件信封与本领域允许的聚合、事件类型。
- `data/sample.json`：一条可用于本地联调的中文样例。
- `src/`：基础校验代码，不包含具体业务流程。
- `tests/`：验证样例与公共字段约定。

事件由 `event_id` 唯一标识，`aggregate_id` 指向业务对象，`version` 从 1 开始递增，`occurred_at` 保留真实发生时间。来源系统重试时必须沿用原事件标识。

## 本地检查

运行 `python3 -m unittest discover -s tests`。
