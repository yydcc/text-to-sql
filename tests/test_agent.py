import json
import os
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from text_to_sql.agent import Agent
from text_to_sql.database import (
    Policy,
    QueryError,
    QueryResult,
    bind_query,
    execute,
    read_connection,
    validate,
)
from text_to_sql.schema import Catalog, Table


@pytest.fixture(autouse=True)
def isolated_records(monkeypatch, tmp_path):
    monkeypatch.setenv("TEXT_TO_SQL_LOG_DIR", str(tmp_path / "runs"))


@pytest.fixture
def catalog():
    return Catalog(
        {
            "public.customers": Table(
                "public.customers",
                [
                    {"name": "id", "type": "integer", "pk": True, "description": "客户编号"},
                    {"name": "city", "type": "text", "pk": False, "description": "客户城市"},
                ],
                [],
                "客户",
            ),
            "public.orders": Table(
                "public.orders",
                [
                    {"name": "id", "type": "integer", "pk": True, "description": "订单号"},
                    {"name": "customer_id", "type": "integer", "pk": False},
                    {
                        "name": "amount",
                        "type": "numeric",
                        "pk": False,
                        "description": "成交金额",
                        "aliases": ["销售额"],
                    },
                    {"name": "status", "type": "text", "pk": False, "description": "paid 已支付"},
                ],
                [{"table": "public.customers", "from": "customer_id", "to": "id"}],
                "订单",
            ),
        }
    )


def model(*responses):
    return FakeListChatModel(
        responses=[
            json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item for item in responses
        ]
    )


POLISH = {
    "polished_question": "上海客户已支付订单的销售额是多少？",
    "intent": "统计销售额",
    "metrics": ["销售额"],
    "dimensions": [],
    "filters": ["上海", "已支付"],
}
LINKS = {
    "tables": ["public.orders", "public.customers"],
    "columns": ["public.orders.amount", "public.customers.city"],
    "mappings": {"销售额": ["public.orders.amount"]},
}
SQL = "SELECT sum(o.amount) AS revenue FROM public.orders o JOIN public.customers c ON c.id=o.customer_id WHERE c.city=:city AND o.status=:status"
DRAFT = {
    "sql": SQL,
    "params": {"city": "上海", "status": "paid"},
    "plan": {"select_items": ["SUM(o.amount)"], "filters": ["上海", "已支付"]},
}


def test_four_stages_and_two_repairs(catalog):
    executor = MagicMock(
        side_effect=[
            QueryError("column o.missing does not exist", "42703"),
            QueryError("operator does not exist", "42883"),
            QueryResult(["revenue"], [[Decimal("2900.00")]], 2.0),
        ]
    )
    repair1 = {**DRAFT, "diagnosis": "列名错误", "changes": ["更换为 amount"], "intent_preserved": True}
    repair2 = {**DRAFT, "diagnosis": "类型错误", "changes": ["修正筛选类型"], "intent_preserved": True}
    events = []
    agent = Agent(
        "secret-dsn",
        catalog=catalog,
        executor=executor,
        model=model(POLISH, LINKS, {**DRAFT, "sql": SQL.replace("o.amount", "o.missing")}, repair1, repair2),
    )
    result = agent.ask("上海已支付销售额？", on_event=events.append)
    assert result["status"] == "ok" and result["repairs_used"] == 2
    assert result["result"]["rows"] == [[Decimal("2900.00")]]
    assert {e["step"] for e in events} == {1, 2, 3, 4}
    assert [e["sqlstate"] for e in events if e.get("status") == "error"] == ["42703", "42883"]
    assert executor.call_args.args[2].allowed_tables == set(catalog.tables)
    assert "secret-dsn" not in json.dumps(events, default=str)


def test_repair_limit_and_empty_result(catalog):
    executor = MagicMock(side_effect=QueryError("missing column", "42703"))
    result = Agent(
        "", catalog=catalog, executor=executor, model=model(POLISH, LINKS, DRAFT), max_repairs=0
    ).ask("销售额")
    assert result["status"] == "error" and executor.call_count == 1
    executor = MagicMock(return_value=QueryResult(["revenue"], [], 1))
    result = Agent("", catalog=catalog, executor=executor, model=model(POLISH, LINKS, DRAFT)).ask("销售额")
    assert result["status"] == "ok" and result["repairs_used"] == 0


def test_partial_query_executes_and_preserves_unknown_amount(catalog):
    query = "SELECT COUNT(*) AS order_count, CAST(NULL AS numeric) AS total_paid FROM public.orders"
    limitation = "没有实际到账金额，无法计算实收总额。"
    executor = MagicMock(return_value=QueryResult(["order_count", "total_paid"], [[4, None]], 1))
    result = Agent(
        "",
        catalog=catalog,
        executor=executor,
        model=model(POLISH, LINKS, {"sql": query, "limitations": [limitation]}),
    ).ask("查询订单数量及实收总额")
    assert result["status"] == "partial"
    assert result["limitations"] == [limitation]
    assert result["result"]["rows"] == [[4, None]]
    executor.assert_called_once()


def test_clarification_and_intent_change(catalog):
    executor = MagicMock(side_effect=QueryError("missing", "42703"))
    missing = {"clarification": "请提供需要统计的客户编号。"}
    result = Agent("", catalog=catalog, executor=executor, model=model(POLISH, LINKS, missing, missing)).ask(
        "指定客户的销售额"
    )
    assert result["status"] == "clarification" and not executor.called
    assert result["answer"] == missing["clarification"]
    assert sum(e.get("phase") == "clarification_review" for e in result["trace"]) == 1
    repair = {**DRAFT, "diagnosis": "无法保持原意", "changes": [], "intent_preserved": False}
    result = Agent("", catalog=catalog, executor=executor, model=model(POLISH, LINKS, DRAFT, repair)).ask(
        "销售额"
    )
    assert result["status"] == "clarification" and executor.call_count == 1


def test_uses_best_supported_interpretation_without_configured_default(catalog):
    question = "按城市统计订单收入"
    assumption = "收入按订单表的成交金额合计。"
    polished = {
        "polished_question": question,
        "intent": "按城市统计订单收入",
        "clarification": "收入是成交金额还是净收入？",
    }
    links = {
        **LINKS,
        "assumptions": [assumption],
        "clarification": "若指净收入，需要成本数据。",
    }
    draft = {
        "sql": "SELECT c.city, SUM(o.amount) FROM public.orders o "
        "JOIN public.customers c ON c.id = o.customer_id GROUP BY c.city",
        "assumptions": [assumption],
    }
    executor = MagicMock(return_value=QueryResult(["city", "revenue"], [["上海", 2900]], 1))
    result = Agent(
        "",
        catalog=catalog,
        model=model(polished, links, {"clarification": polished["clarification"]}, draft),
        executor=executor,
    ).ask(question)

    assert result["status"] == "ok" and result["assumptions"] == [assumption]
    assert result["sql"] == draft["sql"]
    assert not any(e.get("default_assumptions") for e in result["trace"])
    assert any(e.get("phase") == "clarification_review" for e in result["trace"])
    executor.assert_called_once()


def test_bad_format_repaired_within_budget(catalog):
    executor = MagicMock(return_value=QueryResult(["x"], [[1]], 1))
    repair = {**DRAFT, "diagnosis": "JSON格式错误", "changes": ["补齐结构化字段"], "intent_preserved": True}
    result = Agent(
        "", catalog=catalog, executor=executor, model=model(POLISH, LINKS, "not json", repair)
    ).ask("销售额")
    assert result["status"] == "ok" and result["repairs_used"] == 1
    assert executor.call_count == 1


def test_column_recall_and_fk_bridge(catalog):
    items = catalog.recall("销售额", 2)
    assert items[0]["column"] == "public.orders.amount" and items[0]["score"] > 0
    assert catalog.expand(["public.customers", "public.orders"]) == ["public.customers", "public.orders"]


def test_invalid_links(catalog):
    result = Agent(
        "", catalog=catalog, model=model(POLISH, {**LINKS, "columns": ["public.orders.secret"]})
    ).ask("销售额")
    assert result["status"] == "error" and result["failed_stage"] == 2
    assert any(e.get("phase") == "linking_validation" for e in result["trace"])


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE public.orders",
        "SELECT 1; DELETE FROM public.orders",
        "WITH x AS (DELETE FROM public.orders RETURNING *) SELECT * FROM x",
        "SELECT * INTO public.other FROM public.orders",
        "SELECT * FROM public.orders FOR UPDATE",
        "COPY public.orders TO PROGRAM 'id'",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_sleep(100)",
        "SELECT set_config('search_path','public',false)",
        "SELECT nextval('seq')",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT * FROM private.orders",
        "SELECT * FROM orders",
        "SELECT public.sum(amount) FROM public.orders",
        "SELECT amount::public.evil FROM public.orders",
        "SELECT * FROM generate_series(1,10000000)",
        "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT * FROM x",
    ],
)
def test_security_gate(sql):
    with pytest.raises(QueryError):
        validate(sql, {"public.orders"})


def test_postgres_cte_binding_and_safe_literals():
    sql = "WITH x AS (SELECT amount FROM public.orders WHERE status=:status) SELECT sum(amount) FROM x"
    tree = validate(sql, {"public.orders"})
    attack = "paid'; DROP TABLE public.orders; -- %"
    compiled, params = bind_query(tree, {"status": attack})
    assert "$1" in compiled and attack not in compiled and params == [attack]
    with pytest.raises(QueryError, match="match exactly"):
        bind_query(tree, {})
    validate("SELECT sum(CASE WHEN amount > 0 THEN amount ELSE 0 END) FROM public.orders", {"public.orders"})


def test_postgres_date_formatting_and_boolean_predicates_pass_validation():
    query = """
        SELECT TO_CHAR(DATE_TRUNC('month', CAST(policystartdate AS date)), 'YYYY-MM')
        FROM public.policyinfo
        WHERE (policystatus = :active OR policystatus = :suspended)
          AND CAST(policyenddate AS date) <= CAST(policystartdate AS date) + INTERVAL '1 year'
    """
    compiled, params = bind_query(
        validate(query, {"public.policyinfo"}), {"active": "生效", "suspended": "暂停"}
    )
    assert "TO_CHAR(DATE_TRUNC(" in compiled and " AND " in compiled and " OR " in compiled
    assert params == ["生效", "暂停"]


def test_readonly_connection_rollback_and_close(monkeypatch):
    con = MagicMock()
    monkeypatch.setattr("text_to_sql.database.psycopg.connect", MagicMock(return_value=con))
    with pytest.raises(RuntimeError), read_connection("secret", 2):
        assert con.read_only is True
        raise RuntimeError("query failed")
    con.rollback.assert_called_once()
    con.close.assert_called_once()
    statements = [c.args[0] for c in con.execute.call_args_list]
    assert "SET TRANSACTION READ ONLY" in statements
    assert any("statement_timeout" in s for s in statements)
    assert any("search_path" in s for s in statements)


def test_streamed_result_truncation(monkeypatch):
    @contextmanager
    def connection(*args):
        yield MagicMock()

    monkeypatch.setattr("text_to_sql.database.read_connection", connection)
    cursor = MagicMock()
    cursor.description = [SimpleNamespace(name="amount")]
    cursor.fetchmany.return_value = [(1,), (2,), (3,)]
    cursor.__enter__.return_value = cursor
    monkeypatch.setattr("text_to_sql.database.psycopg.RawServerCursor", MagicMock(return_value=cursor))
    result = execute(
        "", "SELECT amount FROM public.orders", Policy(max_rows=2, allowed_tables={"public.orders"})
    )
    assert result.truncated and result.rows == [[1], [2]]
    cursor.__exit__.assert_called_once()


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="No PostgreSQL test connection configured")
def test_existing_postgres_readonly_integration():
    # Read-only test against an existing server. No schema or sample data is created.
    dsn = os.environ["TEST_DATABASE_URL"]
    with read_connection(dsn) as con:
        assert con.execute("SHOW transaction_read_only").fetchone() == ("on",)
    result = execute(dsn, "SELECT CAST(:value AS INTEGER) AS value", Policy(), {"value": 7})
    assert result.rows == [[7]]


def test_cli_live_display_and_json_trace(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from text_to_sql.cli import app

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def ask(self, question, on_event=None):
            trace = [
                {"step": 1, "title": "自然语言润色", "polished_question": question},
                {"step": 2, "title": "候选列召回", "candidates": []},
                {"step": 3, "title": "结构化 SQL 生成", "sql": "SELECT 1"},
                {"step": 4, "title": "执行成功", "status": "ok"},
            ]
            if on_event:
                for event in trace:
                    on_event(event)
            return {
                "status": "ok",
                "answer": "查询返回 1 行。",
                "trace": trace,
                "result": {"columns": ["value"], "rows": [[1]], "truncated": False, "elapsed_ms": 1},
            }

    monkeypatch.setattr("text_to_sql.cli.Agent", FakeAgent)
    monkeypatch.setattr("text_to_sql.cli.settings", lambda *args: {})
    runner = CliRunner()
    result = runner.invoke(app, ["ask", "销售额"])
    assert result.exit_code == 0, result.output
    assert "步骤 1/4" in result.output and "步骤 4/4" in result.output
    target = tmp_path / "trace.json"
    result = runner.invoke(app, ["ask", "销售额", "--json-output", "--trace-file", str(target)])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)["trace"]) == 4
    assert json.loads(target.read_text())["status"] == "ok"


def test_connection_error_does_not_leak_dsn(monkeypatch):
    import psycopg

    @contextmanager
    def unavailable(*args):
        raise psycopg.OperationalError("secret-password@private-server")
        yield  # pragma: no cover -- contextmanager protocol

    monkeypatch.setattr("text_to_sql.database.read_connection", unavailable)
    with pytest.raises(QueryError, match="connection unavailable") as error:
        execute("secret-dsn", "SELECT 1", Policy())
    assert "secret-password" not in str(error.value)


@pytest.fixture
def agent_catalog():
    return Catalog(
        {
            "public.agentinfo": Table(
                "public.agentinfo",
                [
                    {"name": "agentid", "type": "bigint", "pk": False},
                    {"name": "name", "type": "text", "pk": False},
                    {"name": "address", "type": "text", "pk": False},
                ],
                [],
                "",
            )
        }
    )


def test_chinese_surname_retrieval_without_comments(agent_catalog):
    recalled = agent_catalog.recall("姓王的代理人有多少位", 2)
    assert recalled[0]["column"] == "public.agentinfo.name"
    assert recalled[0]["score"] > 0
    assert {"agent", "name"} <= set(recalled[0]["matched_terms"])


def test_empty_links_fallback_then_wang_count(agent_catalog):
    polished = {"polished_question": "统计姓王的代理人人数", "intent": "人数统计", "filters": ["姓王"]}
    links = {"tables": ["public.agentinfo"], "columns": ["public.agentinfo.name", "public.agentinfo.agentid"]}
    draft = {
        "sql": "SELECT COUNT(DISTINCT agentid) FROM public.agentinfo WHERE name LIKE :prefix",
        "params": {"prefix": "王%"},
    }
    executor = MagicMock(return_value=QueryResult(["count"], [[5]], 1))
    result = Agent(
        "",
        catalog=agent_catalog,
        model=model(polished, {"tables": [], "columns": []}, links, draft),
        executor=executor,
    ).ask("姓王的代理人有多少位")
    assert result["status"] == "ok"
    assert result["params"] == {"prefix": "王%"}
    assert any(e.get("reason") == "invalid_or_empty_links" for e in result["trace"])
    assert executor.call_count == 1
    from pathlib import Path

    directory = Path(result["record_dir"])
    for name in [
        "run.json",
        "catalog.json",
        "events.jsonl",
        "result.json",
        "01_question_polish.json",
        "02_column_recall.json",
        "03_sql_generation.json",
        "04_execution_repair.json",
    ]:
        assert (directory / name).exists()
    calls = [json.loads(path.read_text()) for path in sorted((directory / "model_calls").glob("*.json"))]
    assert len(calls) == 4 and all(c["status"] == "ok" for c in calls)
    assert all(
        "input" in c and "raw_output" in c and "parsed_output" in c and "elapsed_ms" in c for c in calls
    )


def test_stage_failure_records_survive_and_skip_later_stages(agent_catalog):
    from pathlib import Path

    broken = model("not json")
    result = Agent("super-secret-dsn", catalog=agent_catalog, model=broken).ask("代理人数量")
    assert result["status"] == "error" and result["failed_stage"] == 1
    directory = Path(result["record_dir"])
    manifest = json.loads((directory / "run.json").read_text())
    assert manifest["stages"] == {"1": "error", "2": "skipped", "3": "skipped", "4": "skipped"}
    assert json.loads((directory / "result.json").read_text())["error"]
    assert "super-secret-dsn" not in "".join(p.read_text() for p in directory.rglob("*.json"))
    call = json.loads(next((directory / "model_calls").glob("*.json")).read_text())
    assert call["status"] == "error" and call["raw_output"] == "not json"


def test_zero_match_uses_bounded_schema_fallback(agent_catalog):
    polished = {"polished_question": "完全不相关的中文问题", "intent": "测试"}
    result = Agent(
        "",
        catalog=agent_catalog,
        model=model(polished, {"clarification": "无法识别业务对象"}, {"clarification": "请说明查询对象"}),
    ).ask("测试")
    assert result["status"] == "clarification"
    assert any(e.get("reason") == "no_positive_lexical_match" for e in result["trace"])
    assert all(e["step"] <= 2 for e in result["trace"])


def test_each_question_has_distinct_record_directory(agent_catalog):
    agent = Agent(
        "",
        catalog=agent_catalog,
        model=model({"polished_question": "澄清", "intent": "澄清", "clarification": "请补充"}),
    )
    first = agent.ask("问一")
    second = agent.ask("问二")
    assert first["run_id"] != second["run_id"] and first["record_dir"] != second["record_dir"]


def test_catalog_initialization_failure_is_recorded(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(
        "text_to_sql.agent.Catalog.load", MagicMock(side_effect=ValueError("No accessible tables"))
    )
    result = Agent("secret-connection", model=model(POLISH)).ask("代理人数量")
    assert result["status"] == "error"
    assert (Path(result["record_dir"]) / "result.json").exists()


def test_entity_to_table_mapping_is_valid_not_a_column_error(agent_catalog):
    polished = {"polished_question": "姓王的代理人有多少位？", "intent": "统计代理人数量"}
    links = {
        "tables": ["public.agentinfo"],
        "columns": ["public.agentinfo.name", "public.agentinfo.agentid"],
        "mappings": {"代理人": ["public.agentinfo"], "姓名": ["public.agentinfo.name"]},
    }
    draft = {
        "sql": "SELECT COUNT(DISTINCT agentid) FROM public.agentinfo WHERE name LIKE :prefix",
        "params": {"prefix": "王%"},
    }
    executor = MagicMock(return_value=QueryResult(["count"], [[5]], 1))
    result = Agent("", catalog=agent_catalog, executor=executor, model=model(polished, links, draft)).ask(
        "姓王的代理人有多少位"
    )
    assert result["status"] == "ok"
    assert not any(e.get("phase") == "linking_validation" for e in result["trace"])


def test_insurance_type_business_mapping_prevents_claim_table(tmp_path):
    from pathlib import Path

    metadata = json.loads((Path(__file__).parent.parent / "data/schema_descriptions.json").read_text())
    catalog = Catalog(
        {
            "public.claiminfo": Table(
                "public.claiminfo",
                [
                    {
                        "name": "claimtype",
                        "type": "text",
                        "description": metadata["public.claiminfo"]["columns"]["claimtype"]["description"],
                        "aliases": metadata["public.claiminfo"]["columns"]["claimtype"]["aliases"],
                    },
                    {
                        "name": "claimamount",
                        "type": "bigint",
                        "description": metadata["public.claiminfo"]["columns"]["claimamount"]["description"],
                        "aliases": metadata["public.claiminfo"]["columns"]["claimamount"]["aliases"],
                    },
                ],
                [],
                metadata["public.claiminfo"]["description"],
            ),
            "public.productinfo": Table(
                "public.productinfo",
                [
                    {
                        "name": name,
                        "type": "bigint" if name == "premium" else "text",
                        **metadata["public.productinfo"]["columns"][name],
                    }
                    for name in ("producttype", "premium", "coveragerange")
                ],
                [],
                metadata["public.productinfo"]["description"],
            ),
        },
        metadata["_concepts"],
    )
    question = "查询每种保险类型的保险金额的平均值、最大值和最小值"
    assert catalog.recall(question, 1)[0]["column"] == "public.productinfo.producttype"
    polished = {"polished_question": question, "intent": "按保险类型聚合"}
    wrong = {
        "tables": ["public.claiminfo"],
        "columns": ["public.claiminfo.claimtype", "public.claiminfo.claimamount"],
    }
    right = {
        "tables": ["public.productinfo"],
        "columns": ["public.productinfo.producttype", "public.productinfo.premium"],
        "clarification": "若业务上指保额区间，则需明确取值规则。",
    }
    draft = {
        "sql": "SELECT producttype, AVG(premium), MAX(premium), MIN(premium) "
        "FROM public.productinfo GROUP BY producttype",
    }
    executor = MagicMock(
        return_value=QueryResult(["producttype", "avg", "max", "min"], [["寿险", 2, 3, 1]], 1)
    )
    result = Agent(
        "",
        catalog=catalog,
        log_dir=tmp_path,
        model=model(polished, wrong, right, {"clarification": "是否按 premium 统计保险金额？"}, draft),
        executor=executor,
    ).ask(question)
    assert result["status"] == "ok"
    assert result["sql"] == draft["sql"]
    assert "保险金额按产品保费 premium 统计。" in result["assumptions"]
    assert any(e.get("reason") == "invalid_or_empty_links" for e in result["trace"])
    assert any("missing_business_columns" in e.get("error", "") for e in result["trace"])
    assert any(e.get("tables") == ["public.productinfo"] for e in result["trace"])
    assert executor.call_count == 1

    # The caller's explicit request for the coverage range overrides the premium default.
    coverage_question = "按保险类型统计保险金额，取保额区间"
    coverage_links = {
        "tables": ["public.productinfo"],
        "columns": ["public.productinfo.producttype", "public.productinfo.coveragerange"],
    }
    coverage = Agent(
        "",
        catalog=catalog,
        log_dir=tmp_path,
        model=model(
            {"polished_question": coverage_question, "intent": "按类型统计保额区间"},
            coverage_links,
            {"clarification": "请明确取保额区间的上限、下限还是中值。"},
            {"clarification": "请明确取保额区间的上限、下限还是中值。"},
        ),
        executor=executor,
    ).ask(coverage_question)
    assert coverage["status"] == "clarification"
    assert not any(e.get("default_assumptions") for e in coverage["trace"])
    assert executor.call_count == 1
