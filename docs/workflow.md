# 四步流程与结构化记录

下面是数据格式示例，表名和字段需要按真实 PostgreSQL 数据库替换。示例不是服务器实测结果。

## 1. 自然语言润色

原问题：“帮我看看上海的，已付款那些订单一共卖了多少钱”。

```json
{
  "step": 1,
  "title": "自然语言润色",
  "polished_question": "上海客户已支付订单的销售额是多少？",
  "intent": "统计指定城市客户的已支付销售额",
  "metrics": ["销售额"],
  "dimensions": [],
  "filters": ["客户城市为上海", "订单为已支付状态"],
  "time_range": "",
  "changes": ["将口语化表达整理为明确的查询问题"],
  "clarification": ""
}
```

润色阶段可以在 `clarification` 中记录待核实的问题，后续阶段结合表结构、已有业务说明和对话上下文判断能否解决。例如“按险种看产品价格”可以根据产品表说明采用保费字段，并在 `assumptions` 中注明。缺少无法推断的编号等输入时，Agent 才会要求补充。

## 2. 候选列召回和链接

列召回读取的是服务器现有元数据，不需要业务建库脚本。例如可召回：

| 列 | 相关线索 |
| --- | --- |
| public.customers.city | 城市、上海 |
| public.orders.status | 已支付、paid |
| public.orders.amount | 成交金额、销售额 |
| public.orders.customer_id | 客户关联键 |

实际 trace 为每列保存 `score` 与 `matched_terms`。之后模型输出：

```json
{
  "tables": ["public.orders", "public.customers"],
  "columns": ["public.orders.amount", "public.orders.status", "public.customers.city"],
  "mappings": {
    "销售额": ["public.orders.amount"],
    "上海客户": ["public.customers.city"],
    "已支付": ["public.orders.status"]
  },
  "rationale": "金额、支付状态和客户城市共同决定本次查询"
}
```

`mappings` 也允许实体映射到表，例如 `"代理人": ["public.agentinfo"]`，不会将合法表映射错误地当成未知列。字段与表名分别对真实 schema 校验；连接关系来自真实外键。若表之间没有声明外键，模型只能依靠表列定义与业务描述推断，需人工检查 JOIN 关系。

## 3. 结构化 SQL 生成

```json
{
  "plan": {
    "select_items": ["SUM(o.amount) AS revenue"],
    "joins": ["public.orders o JOIN public.customers c ON c.id = o.customer_id"],
    "filters": ["c.city = :city", "o.status = :status"],
    "group_by": [],
    "order_by": [],
    "limit": null
  },
  "sql": "SELECT SUM(o.amount) AS revenue FROM public.orders o JOIN public.customers c ON c.id = o.customer_id WHERE c.city = :city AND o.status = :status",
  "params": {"city": "上海", "status": "paid"},
  "assumptions": ["依据已提供元数据，amount 为成交金额，paid 为已支付状态"],
  "clarification": ""
}
```

`plan` 用于人工检查，不是另一套查询执行器；实际执行的是 `sql` 与绑定参数。模型被要求保持两者一致，但目前没有独立的语义等价证明。

## 4. 多轮执行自纠

以生成 SQL 错用了 `o.total_amount` 为例：

1. 第 0 轮：AST 检查，开启 PostgreSQL 只读事务，执行 SQL；服务器返回 `42703` 未定义列。记录 SQL、参数、错误摘要和耗时，回滚并关闭连接。
2. 第 1 轮修复：模型接收原始问题、润色意图、真实 schema、上一版结构化草案及所有失败记录，输出 `diagnosis`、`changes`、`intent_preserved` 和完整新草案。
3. 重新安全校验，用新连接执行修复 SQL。若仍有错误，继续下一轮；不会在已失败事务中直接重试。
4. 成功、超出修复预算、关键歧义、模型报告语义改变、或连接不可用时停止。

典型修复输出在 Draft 基础上追加：

```json
{
  "diagnosis": "orders 表没有 total_amount，元数据中成交金额列为 amount",
  "changes": ["将 o.total_amount 改为 o.amount；保留城市和支付状态条件"],
  "intent_preserved": true
}
```

最终结果包含 `status`、`answer`、`sql`、`params`、`assumptions`、`repairs_used`、`result` 与完整 `trace`。首次执行轮次为 0，修复从 1 开始。若初次即成功，第四步仍会输出“执行成功”，修复次数为 0；不会人为制造错误来展示自纠。

可以通过 `--repairs 0` 关闭修复，对比相同问题在修复前后的表现。


## 自动记录与失败诊断

每个问题对应一个独立运行目录。开始时四个阶段文件均已创建，初始为 `pending`；开始执行后为 `running`，结束为 `ok`、`clarification`、`error` 或 `invalid_format`。未到达的阶段在汇总时为 `skipped`。

- 怀疑润色改变原意：比较 `run.json` 原问题与 `01_question_polish.json` 的输出。
- 怀疑字段没召回：查看 `02_column_recall.json` 中的检索词、分数、匹配词、中英扩展词及兜底候选表。
- 怀疑模型选对却校验失败：对照该阶段 `model_calls` 的 `raw_output` / `parsed_output` 与 `linking_validation` 中的未知表、未知列和未知映射列表。
- 怀疑 SQL 生成错误：检查 `03_sql_generation.json` 的输入 schema、plan、SQL 与参数。
- 怀疑自纠丢失条件：比较 `04_execution_repair.json` 内每轮 SQL、params、SQLSTATE、诊断和改动，以及对应模型调用输入中的 `failed_rounds`。

对“姓王的代理人有多少位”，召回应优先匹配代理人实体与姓名列；“王”属于筛选值而非字段名。姓名中保存完整中文姓名时，应生成 `name LIKE :prefix`，绑定 `prefix="王%"`；若数据库独立保存姓氏，则按真实字段做等值筛选。不会为了展示自纠而人为制造执行错误。

## 保险产品类型误召回的回归案例

2026-11-23 的失败记录中，问题“查询每种保险类型的保险金额的平均值、最大值和最小值”只把 `public.claiminfo.claimamount` 排为正分，`public.productinfo` 被前 20 列的零分结果挤出。模型于是将“保险类型”误映射为 `claimtype`，把“保险金额”误映射为 `claimamount`。真实表结构中：

| 业务词 | 实际列 | 含义 |
| --- | --- | --- |
| 保险类型 | `public.productinfo.producttype` | 产品/险种分类 |
| 保费 | `public.productinfo.premium` | 数值型保费金额 |
| 保额区间 | `public.productinfo.coveragerange` | 文本型保障范围，如 `100万-500万` |
| 理赔类型 | `public.claiminfo.claimtype` | 理赔分类，不能代替险种 |
| 理赔金额 | `public.claiminfo.claimamount` | 赔付/理赔金额，不能代替保费或保额 |

已生成 [数据库表列说明](../data/schema_descriptions.json)：覆盖本机当前 7 张表及其全部列，每列提供中文名称和结合所在表的业务说明。`_concepts` 是可审计的业务概念约束：“保险类型”必须链接到 `producttype`。召回时列描述与别名优先于泛化的表级词；精确业务别名与概念约束得到额外分数。模型返回链接后还会校验必需列，错误链接触发扩展 schema 重试，完整保存于第二阶段记录。

“保险金额”的默认口径是产品保费 `premium`。原问题可以直接从 `productinfo` 按 `producttype` 分组执行 AVG/MAX/MIN，结果和轨迹会注明采用的口径。用户明确要求统计保额区间时，使用 `coveragerange` 的业务含义，并在缺少上限、下限或其他取值规则时澄清。

在 `_concepts` 中，`columns` 的每个数组元素对应一个完整列名。`default_assumption` 指定默认口径及结果说明，`unless_terms` 列出会暂缓采用该口径的明确表述。更新配置后重新进入 `chat`，以加载新版本。

默认 CLI 会加载 `data/schema_descriptions.json`。测试其他数据库时，设置 `TEXT_TO_SQL_METADATA_PATH` 指向对应库的说明文件；也可以通过 `--metadata` 指定。数据库结构或业务口径变化后，请同步更新此说明文件。
