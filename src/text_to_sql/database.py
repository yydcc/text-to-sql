"""PostgreSQL read-only execution with AST validation and bounded server cursors."""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope


class QueryError(ValueError):
    def __init__(self, message: str, sqlstate: str | None = None):
        super().__init__(message)
        self.sqlstate = sqlstate


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    elapsed_ms: float
    truncated: bool = False


@dataclass
class Policy:
    timeout_s: float = 10.0
    max_rows: int = 200
    max_bytes: int = 1_000_000
    allowed_tables: set[str] = field(default_factory=set)


SAFE_FUNCTIONS = {
    "CASE",
    "IF",
    "ABS",
    "AVG",
    "COUNT",
    "SUM",
    "MIN",
    "MAX",
    "ROUND",
    "COALESCE",
    "NULLIF",
    "LOWER",
    "UPPER",
    "LENGTH",
    "SUBSTRING",
    "TRIM",
    "LTRIM",
    "RTRIM",
    "REPLACE",
    "CAST",
    "TRY_CAST",
    "EXTRACT",
    "TIMESTAMP_TRUNC",
    "DATE_TRUNC",
    "DATE",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "CURRENT_TIME",
    "NOW",
    "TO_CHAR",
    "TO_DATE",
    "CEIL",
    "FLOOR",
    "POWER",
    "SQRT",
    "MOD",
    "SIGN",
    "ROW_NUMBER",
    "RANK",
    "DENSE_RANK",
    "PERCENT_RANK",
    "CUME_DIST",
    "NTILE",
    "LAG",
    "LEAD",
    "FIRST_VALUE",
    "LAST_VALUE",
    "NTH_VALUE",
    "ARRAY_AGG",
    "STRING_AGG",
    "GROUP_CONCAT",
    "CONCAT",
    "CONCAT_WS",
    "BOOL_AND",
    "BOOL_OR",
    "STDDEV",
    "STDDEV_POP",
    "STDDEV_SAMP",
    "VAR_POP",
    "VAR_SAMP",
    "GREATEST",
    "LEAST",
}


def validate(sql: str, allowed_tables: set[str]) -> exp.Expression:
    if not sql.strip() or len(sql) > 50_000:
        raise QueryError("SQL must contain 1–50000 characters")
    try:
        trees = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.SqlglotError as exc:
        raise QueryError(f"PostgreSQL parse error: {exc}") from exc
    if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise QueryError("Only a single SELECT query is permitted")
    tree = trees[0]
    forbidden = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Merge,
        exp.Create,
        exp.Drop,
        exp.Command,
        exp.Into,
        exp.Lock,
        exp.Copy,
        exp.Parameter,
    )
    if any(isinstance(node, forbidden) for node in tree.walk()):
        raise QueryError("Writes, locks, commands and native $ parameters are forbidden; use :name")
    if any(cte.args.get("recursive") for cte in tree.find_all(exp.With)):
        raise QueryError("Recursive CTEs are disabled")
    for node in tree.find_all(exp.Func):
        # SQLGlot represents Boolean operators and PostgreSQL TO_CHAR as function nodes.
        if isinstance(node, (exp.And, exp.Or, exp.TimeToStr)) and not isinstance(node.parent, exp.Dot):
            continue
        name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
        if name not in SAFE_FUNCTIONS or isinstance(node.parent, exp.Dot):
            raise QueryError(f"Function not approved: {name}")
    for dtype in tree.find_all(exp.DataType):
        if dtype.this == exp.DataType.Type.USERDEFINED:
            raise QueryError("User-defined cast types are disabled")
    # Scope resolution distinguishes physical relations from CTE/subquery aliases.
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, Scope):
                continue
            if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
                raise QueryError("Table functions are disabled")
            if source.catalog or not source.db:
                raise QueryError("Physical tables must use schema.table qualification")
            parts = [part.name if part.args.get("quoted") else part.name.lower() for part in source.parts]
            if ".".join(parts) not in allowed_tables:
                raise QueryError(f"Table outside allowlist: {'.'.join(parts)}")
    return tree


def bind_query(tree: exp.Expression, params: dict) -> tuple[str, list]:
    """Convert named AST placeholders into PostgreSQL $n, never interpolate values."""
    names = list(dict.fromkeys(node.name for node in tree.find_all(exp.Placeholder)))
    if set(names) != set(params) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", n) for n in names):
        raise QueryError("SQL placeholders and params keys must match exactly")
    positions = {name: index + 1 for index, name in enumerate(names)}
    compiled = tree.transform(
        lambda n: (
            exp.Parameter(this=exp.Literal.number(positions[n.name])) if isinstance(n, exp.Placeholder) else n
        )
    )
    return compiled.sql(dialect="postgres"), [params[name] for name in names]


@contextmanager
def read_connection(dsn: str, timeout_s: float = 10.0):
    """Always roll back and close, including after errors and truncated reads."""
    con = psycopg.connect(dsn, connect_timeout=5, application_name="text_to_sql_agent")
    try:
        con.read_only = True
        con.execute("SET TRANSACTION READ ONLY")
        milliseconds = str(max(1, int(timeout_s * 1000)))
        con.execute("SELECT set_config('statement_timeout', %s, true)", (milliseconds,))
        con.execute("SELECT set_config('lock_timeout', %s, true)", (str(min(2000, int(milliseconds))),))
        con.execute("SELECT set_config('idle_in_transaction_session_timeout', '15000', true)")
        con.execute("SELECT set_config('search_path', 'pg_catalog', true)")
        yield con
    finally:
        try:
            con.rollback()
        finally:
            con.close()


def execute(dsn: str, sql: str, policy: Policy, params: dict | None = None) -> QueryResult:
    if policy.timeout_s <= 0 or policy.max_rows < 1 or policy.max_bytes < 1:
        raise QueryError("Invalid execution budget")
    tree = validate(sql, policy.allowed_tables)
    compiled, values = bind_query(tree, params or {})
    try:
        with read_connection(dsn, policy.timeout_s) as con:
            started = time.monotonic()
            rows, size, truncated = [], 0, False
            # Server-side storage avoids downloading an unbounded result into client memory.
            with psycopg.RawServerCursor(con, name="agent_result") as cur:
                cur.execute(compiled, values)
                columns = [col.name for col in cur.description]
                while True:
                    if time.monotonic() - started > policy.timeout_s:
                        raise QueryError("Overall query/read timeout exceeded", "57014")
                    batch = cur.fetchmany(min(100, policy.max_rows + 1 - len(rows)))
                    if not batch:
                        break
                    for row in batch:
                        size += len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
                        if len(rows) == policy.max_rows or size > policy.max_bytes:
                            truncated = True
                            break
                        rows.append(list(row))
                    if truncated:
                        break
                return QueryResult(columns, rows, (time.monotonic() - started) * 1000, truncated)
    except psycopg.Error as exc:
        # Connection messages may contain credentials/host details: keep them out of LLM traces.
        if exc.sqlstate is None or exc.sqlstate.startswith("08"):
            raise QueryError(
                "PostgreSQL connection unavailable; check DATABASE_URL and network", exc.sqlstate
            ) from None
        message = exc.diag.message_primary or "PostgreSQL query failed"
        raise QueryError(message[:800], exc.sqlstate) from None
