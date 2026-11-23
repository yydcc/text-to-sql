import json
import os
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table as RichTable
from rich.text import Text

from .agent import Agent
from .schema import Catalog

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
console = Console(highlight=False)


def emit(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def settings(metadata=None, tables=None, schemas=None):
    load_dotenv()
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        raise typer.BadParameter("请在 .env 中配置 DATABASE_URL（PostgreSQL 连接串）")
    schema_list = [s.strip() for s in (schemas or os.getenv("PG_SCHEMAS", "public")).split(",") if s.strip()]
    table_text = tables if tables is not None else os.getenv("PG_ALLOWED_TABLES", "")
    allowed = {s.strip() for s in table_text.split(",") if s.strip()} or None
    metadata_path = metadata or (
        Path(os.getenv("TEXT_TO_SQL_METADATA_PATH", "data/schema_descriptions.json"))
        if Path(os.getenv("TEXT_TO_SQL_METADATA_PATH", "data/schema_descriptions.json")).exists()
        else None
    )
    return {
        "dsn": dsn,
        "schemas": schema_list,
        "allowed": allowed,
        "metadata": json.loads(metadata_path.read_text()) if metadata_path else None,
    }


def display_event(event):
    step, title = event["step"], event["title"]
    console.rule(f"[bold cyan]步骤 {step}/4 · {title}[/bold cyan]")
    data = {k: v for k, v in event.items() if k not in {"step", "title", "sql", "result"}}
    console.print(Panel(Text(json.dumps(data, ensure_ascii=False, indent=2, default=str))))
    if event.get("sql"):
        console.print(Syntax(event["sql"], "sql", word_wrap=True))


def display_result(result):
    answer = result.get("answer", result.get("error", ""))
    console.print(f"\n状态：{result['status']} · {answer}", markup=False)
    for assumption in result.get("assumptions", []):
        if assumption not in answer:
            console.print(f"查询口径 · {assumption}", markup=False)
    for limitation in result.get("limitations", []):
        console.print(f"数据限制 · {limitation}", markup=False)
    if result.get("record_dir"):
        console.print(f"记录目录：{result['record_dir']}", markup=False)
    if result.get("result"):
        table = RichTable(show_lines=True)
        for name in result["result"]["columns"]:
            table.add_column(name)
        for row in result["result"]["rows"]:
            table.add_row(*("NULL" if v is None else str(v) for v in row))
        console.print(table)


@app.command()
def inspect(metadata: Path | None = None, tables: str | None = None, schemas: str | None = None):
    """Read existing PostgreSQL schema, columns, comments and foreign keys (no model call)."""
    config = settings(metadata, tables, schemas)
    catalog = Catalog.load(**config)
    emit(catalog.render(sorted(catalog.tables)))


@app.command()
def ask(
    question: str,
    metadata: Path | None = None,
    tables: str | None = None,
    schemas: str | None = None,
    repairs: Annotated[int, typer.Option(min=0, max=10)] = 3,
    top_k: Annotated[int, typer.Option(min=1)] = 20,
    timeout: Annotated[float, typer.Option(min=0.1)] = 10.0,
    max_rows: Annotated[int, typer.Option(min=1)] = 200,
    json_output: bool = False,
    trace_file: Path | None = None,
    log_dir: Path | None = None,
):
    """Run and display all four stages; --json-output emits machine-readable JSON only."""
    agent = Agent(
        **settings(metadata, tables, schemas),
        max_repairs=repairs,
        top_k=top_k,
        timeout=timeout,
        max_rows=max_rows,
        log_dir=log_dir,
    )
    result = agent.ask(question, on_event=None if json_output else display_event)
    if trace_file:
        trace_file.parent.mkdir(parents=True, exist_ok=True)
        trace_file.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if json_output:
        emit(result)
    else:
        display_result(result)
    if result["status"] == "error":
        raise typer.Exit(1)


@app.command()
def chat(
    metadata: Path | None = None,
    tables: str | None = None,
    schemas: str | None = None,
    repairs: Annotated[int, typer.Option(min=0, max=10)] = 3,
    top_k: Annotated[int, typer.Option(min=1)] = 20,
    log_dir: Path | None = None,
):
    """Interactive questions/clarifications with four-stage live traces; type exit to stop."""
    agent = Agent(**settings(metadata, tables, schemas), max_repairs=repairs, top_k=top_k, log_dir=log_dir)
    history = []
    while True:
        try:
            question = typer.prompt("问题（exit 退出）")
        except (EOFError, typer.Abort):
            break
        if question.strip().lower() in {"exit", "quit"}:
            break
        result = agent.ask(question, history=history, on_event=display_event)
        display_result(result)
        history.extend(
            [
                {"role": "user", "content": question},
                {
                    "role": "assistant",
                    "sql": result.get("sql"),
                    "params": result.get("params"),
                    "assumptions": result.get("assumptions", []),
                    "limitations": result.get("limitations", []),
                    "answer": result.get("answer", result.get("error")),
                },
            ]
        )
        history = history[-6:]


if __name__ == "__main__":
    app()
