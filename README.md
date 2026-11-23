# PostgreSQL Text-to-SQL Agent

基于 **uv + LangChain + PostgreSQL**，连接服务器上已有数据库，展示完整四步流程：

**自然语言润色 → 候选列召回 / schema linking → 结构化 SQL 生成 → 多轮执行自纠**。

项目提供 CLI 和 Python API；不创建数据库、表或样例业务数据。BIRD 评测暂不纳入当前版本。

## 配置与运行

```bash
uv sync --python 3.11
cp .env.example .env
```

修改 `.env`：

```dotenv
DATABASE_URL=postgresql://readonly_user:password@your-server:5432/your_database?sslmode=require
PG_SCHEMAS=public
PG_ALLOWED_TABLES=public.orders,public.customers
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-name
# OPENAI_BASE_URL=https://your-provider.example/v1
```

`DATABASE_URL` 采用 psycopg/libpq PostgreSQL 连接串，不使用 `postgresql+psycopg://`。用户名和密码中的特殊字符需要 URL 编码。TLS 配置按服务器要求填写，例如使用 `verify-full` 与 CA 证书验证服务端身份；也可通过 SSH 隧道连接本地转发端口。服务器账号需有目标 schema 的 USAGE 及业务表的 SELECT 权限。

`PG_SCHEMAS` 为逗号分隔的 schema 名称；`PG_ALLOWED_TABLES` 是可选的 `schema.table` 白名单，留空表示配置 schema 内当前账号可读取的普通表及分区表。当前不支持视图/物化视图；建议初次接入显式指定业务表。

先验证连接与元数据读取（不调用 LLM）：

```bash
uv run text-to-sql inspect
```

再执行单次查询或多轮对话：

```bash
uv run text-to-sql ask '上海客户已支付订单的销售额是多少？'
uv run text-to-sql chat
```

以上问题和配置中的表名仅作格式示例，请替换为实际数据库业务。运行前必须将 `.env` 中的示例地址、账号和密钥替换为真实配置。

默认逐步展示每一步的结构化内容、SQL、错误码、修复说明及最终数据表。`chat` 保留最近 3 轮上下文，可继续回答澄清问题，或追问“改成按月统计”。

```bash
# 加入业务同义词与指标口径，扩展召回到前 30 列，最多修复 4 轮
uv run text-to-sql ask '按月统计已支付销售额' \
  --metadata data/metadata.json --top-k 30 --repairs 4 \
  --trace-file reports/query.json

# 给程序使用：仅输出 JSON，包含完整 trace
uv run text-to-sql ask '各城市客户数量' --json-output

# 命令行配置覆盖环境中的 schema/表范围
uv run text-to-sql inspect --schemas sales --tables sales.orders,sales.customers
```

## 四步如何实现

| 步骤 | 输入和处理 | 可观察输出 |
| --- | --- | --- |
| 1. 自然语言润色 | 原始问题、近期对话、当前日期和时区；LLM 整理语句、消解指代，提取查询意图；不补造业务条件 | `polished_question`、`intent`、`metrics`、`dimensions`、`filters`、`time_range`、`changes`、`clarification` |
| 2. 候选列召回 | 从服务器读取真实表/列、类型、主键、外键与注释；结合自定义描述/别名进行列级词法 TF/IDF 召回；LLM 完成概念到列的映射；外键最短路径补齐 JOIN 中间表 | 每列的全名、类型、得分和匹配词；最终 `tables`、`columns`、`mappings`、选择理由和补全表 |
| 3. 结构化 SQL 生成 | 润色后的意图、召回/链接结果和完整所选表定义；LangChain Prompt → ChatModel → PydanticOutputParser | `plan`（投影、JOIN、筛选、分组、排序、LIMIT）、PostgreSQL `sql`、`params`、业务假设或澄清问题 |
| 4. 多轮执行自纠 | 安全检查并执行 SQL；将 SQLSTATE、错误摘要、上一版草案、完整失败历史与原始意图传回模型；每轮重新校验并开新事务 | 第 0 轮执行、每轮失败 SQL/参数/错误码、`diagnosis`、`changes`、`intent_preserved`、修复 SQL、最终结果及修复次数 |

默认最多修复 3 次，即初次执行 + 最多 3 次修复后的执行。第 3 步或修复阶段的 JSON 格式错误同样消耗修复预算；润色失败会保存错误并停止。链接为空、非法或格式错误时，扩大候选 schema 重试一次，仍失败则保存失败原因或返回澄清。连接不可用时停止重试 SQL。空结果正常返回，不触发放宽条件。

默认采用证据最充分的可行解释。显式要求优先，其次是适用的业务默认口径，再依据上下文、列说明、别名和数据类型选择。润色时提出的疑问留给后续阶段结合表结构判断。生成阶段若仍建议澄清，会复核一次。只有缺少可信解释或必要输入时才向用户询问。采用的推断随结果显示，并写入 `assumptions`。

模型在修复时报告无法保留原意，会停止并要求澄清。`intent_preserved` 是模型自报标志，不能保证检测所有语义偏移。成功执行的查询仍可能需要人工核对口径。

详细的数据格式与流程示例见 [workflow.md](docs/workflow.md)。

## 每次提问的阶段记录

`ask`、`chat` 和 Python `Agent.ask()` **默认自动落盘**，无需加 `--trace-file`。每次提问都会生成唯一 `run_id`，并返回/显示绝对 `record_dir`。默认根目录是 `reports/runs/`，可通过 `--log-dir /path/to/runs` 或 `.env` 中的 `TEXT_TO_SQL_LOG_DIR` 修改。

```text
reports/runs/<时间_唯一ID>/
  run.json                   # 原始问题、有限对话历史、运行参数、阶段状态与时间
  catalog.json               # 本次可见 schema 快照
  01_question_polish.json    # 问题润色的输入、输出事件、状态和耗时
  02_column_recall.json      # 检索词、分数、扩展词、候选列、链接与校验/兜底记录
  03_sql_generation.json     # 生成输入、结构化草案、SQL/参数、格式错误
  04_execution_repair.json   # 所有执行与修复轮次、SQLSTATE、最终数据
  model_calls/               # 每次模型调用的输入、输出 schema、原始响应、解析结果、usage/耗时
  events.jsonl               # 按事件追加的时间线，可用于实时查看
  result.json                # 最终返回值与 trace
```

四个阶段文件在提问开始时即创建，并随着执行逐步更新；中途失败仍保留已完成内容。未进入的阶段标记 `skipped`，不会伪造后续输出。失败结果包含 `failed_stage`（阶段异常或链接失败时）及记录目录；模型网络错误、格式错误和元数据加载错误也会被保存，`chat` 可继续下一次提问。`--trace-file` 仍用于额外导出单个汇总 JSON。

`catalog.json` 在元数据成功读取后生成。模型原始响应可用于区分“模型未识别字段”和“程序校验拒绝了正确字段”；列召回记录中的 `query_expansions`、`positive_matches`、`matched_terms` 可用于分析检索问题。

```bash
uv run text-to-sql ask '姓王的代理人有多少位' --log-dir reports/runs
uv run text-to-sql chat --log-dir reports/manual-tests
```

记录会包含问题、参数、可见 schema 和查询结果；连接串/API Key 会脱敏，运行目录和文件使用当前用户访问权限，并被 `.gitignore` 排除。各阶段耗时包括本阶段程序处理与记录写入；模型调用文件另提供单次调用耗时。`model_calls` 中的 `usage` 依赖模型提供方是否返回 token 使用量。

## 元数据与候选列召回

首次连接读取 PostgreSQL 系统目录，拿到业务表的真实结构和 COMMENT；不对业务行做采样。`data/schema_descriptions.json` 是根据本机现有 7 张业务表整理的默认说明文件（通过 `TEXT_TO_SQL_METADATA_PATH` 或 `--metadata` 可替换）；`data/metadata.json` 是通用配置模板，需改成实际 `schema.table` 与列名，可补充中文描述、枚举值、指标口径和同义词。不存在的模板表不会创建或加入候选。

```json
{
  "sales.orders": {
    "description": "销售订单，统计收入时仅计 paid 已支付订单",
    "columns": {
      "amount": {"description": "成交金额，元", "aliases": ["销售额", "成交额"]},
      "created_at": "下单时间，服务器存储 UTC 时间"
    }
  }
}
```

`--top-k` 控制列召回数量，默认 20。支持英文词、下划线/驼峰拆词、中文字符/二元词及中英概念扩展（如“代理人”→ agent/broker，“姓氏筛选”→ name/surname）。同时识别 agentinfo 等连续英文字段中的词根。候选列得分是词法相关性，不是置信概率。LLM 可以从召回列所属表中补选其他列；主外键关系再用于表路径补全。生成阶段发送最终表的完整列定义，方便保留 JOIN 键和后续修复。零分召回或链接校验失败时，会扩大到最多 400 列对应的已授权表进行语义兜底，记录触发原因与省略的表，不会把零分包装成匹配成功。概念映射允许实体指向表、属性指向列，且都必须来自已授权 schema。

召回依赖列名、注释和别名质量；大型 schema 可提高 top-k 或接入 embedding/BM25 检索。本版没有向量库依赖，也不搜索原始业务值。`chat` 会复用启动时读取的 schema；服务端变更结构后应重启会话刷新。

## PostgreSQL 执行边界

- 使用 sqlglot 按 PostgreSQL 方言解析，只允许单条 SELECT/非递归 CTE/集合查询；拒绝写操作、SELECT INTO、行锁、COPY、系统/越权表和非白名单函数。
- 物理表必须带 schema，通过作用域解析区分真实表与 CTE 别名；执行范围限制为链接后所选表。
- `:name` 参数由 AST 转成 PostgreSQL `$n`，值通过 psycopg 参数绑定发送；不会拼接进 SQL。执行规范化后的 AST。
- 每轮新连接、只读事务；限制 statement_timeout、lock_timeout，固定 `search_path=pg_catalog`，结束时始终 rollback 并关闭连接。失败事务不会被下一轮复用。实现依据 [PostgreSQL 只读事务文档](https://www.postgresql.org/docs/current/sql-set-transaction.html) 和 [psycopg 事务管理](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)。
- 使用 [RawServerCursor](https://www.psycopg.org/psycopg3/docs/advanced/cursors.html) 分批取数，默认最多返回 200 行、1 MB；截断会显式标记，不改写业务 SQL。`--timeout` 默认 10 秒，限制服务端每条语句并在分批读取间检查总耗时；不承诺网络中断场景下的严格端到端截止时间。连接超时为 5 秒。
- AST 和函数白名单属于应用防护，最终权限依赖服务器只读账号。自定义类型/函数、RLS/触发的数据库逻辑及服务端扩展需由数据库管理员管理；本项目不提供行级租户隔离、脱敏、OS 沙箱或生产服务鉴权。系统目录只由可信元数据读取代码访问。

发送给 LLM 的内容包括用户问题、历史、可见 schema、注释/业务定义和必要的查询错误；不发送连接串和查询结果。终端和 `--trace-file` 可能包含用户输入、SQL 参数及结果，保存时注意其数据敏感性。

## 验证与代码位置

```bash
uv run ruff check src tests
uv run pytest -q

# 可选：针对已有服务器只执行 SELECT 与只读状态检查，不创建任何数据
TEST_DATABASE_URL='postgresql://...' uv run pytest -q -k existing_postgres
```

无服务器配置时，PostgreSQL 集成测试会跳过；本地自动测试主要使用结构化模型响应及数据库 mock 验证流程与边界；不能将 mock 测试通过率视为模型 SQL 正确率。当前另已通过 `.env` 指定服务器对“姓王的代理人有多少位”做真实回归，完整记录保存在本地 `reports/runs/`，查询结果只代表执行当时的数据快照。

- `src/text_to_sql/agent.py`：四步流程、结构化输出和有界自纠。
- `src/text_to_sql/schema.py`：服务器元数据、列召回和外键补全。
- `src/text_to_sql/database.py`：PostgreSQL 安全校验、绑定参数和只读执行。
- `src/text_to_sql/cli.py`：inspect / ask / chat、实时展示和 trace 导出。
- `src/text_to_sql/recording.py`：每次问题的增量记录、阶段文件、模型响应与错误归档。
- `tests/test_agent.py`：流程、安全、事务清理、展示和可选集成测试。

保险类型误召回的表结构对照、修复方式及真实回归见 [四步流程说明](docs/workflow.md#保险产品类型误召回的回归案例)。
