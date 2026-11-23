from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import asdict, dataclass

from psycopg.rows import dict_row

from .database import read_connection


@dataclass
class Table:
    name: str
    columns: list[dict]
    foreign_keys: list[dict]
    description: str = ""


def terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text.lower())
    result = set()
    for word in words:
        if re.search(r"[\u4e00-\u9fff]", word):
            result.update(word)
            result.update(word[i : i + 2] for i in range(len(word) - 1))
        else:
            result.add(word)
    return result


# Query-side bilingual expansion; these are retrieval hints, not invented schema aliases.
CONCEPTS = {
    "代理人": ("agent", "broker", "representative"),
    "代理商": ("agent", "broker"),
    "姓名": ("name", "fullname", "surname", "lastname", "firstname"),
    "姓氏": ("name", "surname", "lastname", "familyname"),
    "客户": ("customer", "client"),
    "保单": ("policy", "insurance"),
    "受益人": ("beneficiary",),
    "理赔": ("claim",),
    "销售额": ("amount", "revenue", "sales"),
    "金额": ("amount", "total", "price"),
    "日期": ("date", "time"),
    "地址": ("address",),
    "电话": ("phone", "telephone"),
}
ENGLISH_HINTS = {term for values in CONCEPTS.values() for term in values}


def expanded_query(text):
    result = terms(text)
    matched = {}
    for concept, aliases in CONCEPTS.items():
        if concept in text:
            result.update(aliases)
            matched[concept] = list(aliases)
    if re.search(r"姓[\u4e00-\u9fff]", text):
        aliases = ["name", "surname", "lastname", "familyname"]
        result.update(aliases)
        matched["姓氏筛选"] = aliases
    return result, matched


def schema_terms(text):
    tokens = terms(re.sub(r"([a-z])([A-Z])", r"\1 \2", text))
    # Flattened identifiers such as agentinfo / agentname still expose their concept.
    for token in list(tokens):
        for hint in ENGLISH_HINTS:
            if token.startswith(hint) or token.endswith(hint):
                tokens.add(hint)
    return tokens


class Catalog:
    def __init__(self, tables: dict[str, Table], concepts: dict | None = None):
        self.tables = tables
        self.concepts = concepts or {}
        if not tables:
            raise ValueError("No accessible tables found in configured schemas/allowlist")

    @classmethod
    def load(cls, dsn: str, schemas: list[str], metadata=None, allowed=None):
        metadata = metadata or {}
        tables = {}
        with read_connection(dsn) as con, con.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT n.nspname AS schema_name, c.relname AS table_name,
                       obj_description(c.oid, 'pg_class') AS table_comment,
                       a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS data_type,
                       col_description(c.oid, a.attnum) AS column_comment,
                       EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid=c.oid
                               AND i.indisprimary AND a.attnum=ANY(i.indkey)) AS pk
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_attribute a ON a.attrelid=c.oid
                WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p')
                  AND a.attnum>0 AND NOT a.attisdropped
                  AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT')
                ORDER BY n.nspname, c.relname, a.attnum
            """,
                (schemas,),
            )
            for row in cur.fetchall():
                name = f"{row['schema_name']}.{row['table_name']}"
                if allowed is not None and name not in allowed:
                    continue
                info = metadata.get(name, {})
                table = tables.setdefault(
                    name, Table(name, [], [], info.get("description") or row["table_comment"] or "")
                )
                column_info = info.get("columns", {}).get(row["column_name"], {})
                if isinstance(column_info, str):
                    column_info = {"description": column_info}
                table.columns.append(
                    {
                        "name": row["column_name"],
                        "type": row["data_type"],
                        "pk": row["pk"],
                        "description": column_info.get("description") or row["column_comment"] or "",
                        "aliases": column_info.get("aliases", []),
                    }
                )
            cur.execute(
                """
                SELECT sn.nspname||'.'||s.relname AS source_table, sa.attname AS source_column,
                       tn.nspname||'.'||t.relname AS target_table, ta.attname AS target_column,
                       k.conname AS constraint_name
                FROM pg_constraint k JOIN pg_class s ON s.oid=k.conrelid
                JOIN pg_namespace sn ON sn.oid=s.relnamespace
                JOIN pg_class t ON t.oid=k.confrelid JOIN pg_namespace tn ON tn.oid=t.relnamespace
                JOIN LATERAL unnest(k.conkey,k.confkey) AS keys(src,dst) ON true
                JOIN pg_attribute sa ON sa.attrelid=s.oid AND sa.attnum=keys.src
                JOIN pg_attribute ta ON ta.attrelid=t.oid AND ta.attnum=keys.dst
                WHERE k.contype='f' AND sn.nspname=ANY(%s)
                ORDER BY source_table, constraint_name, source_column
            """,
                (schemas,),
            )
            for row in cur.fetchall():
                if row["source_table"] in tables and row["target_table"] in tables:
                    tables[row["source_table"]].foreign_keys.append(
                        {
                            "table": row["target_table"],
                            "from": row["source_column"],
                            "to": row["target_column"],
                            "constraint": row["constraint_name"],
                        }
                    )
        if allowed is not None and not allowed <= set(tables):
            raise ValueError(f"Inaccessible or unknown allowed tables: {sorted(allowed - set(tables))}")
        return cls(tables, metadata.get("_concepts", {}))

    def matching_concepts(self, question: str, *, original_question: str | None = None) -> dict:
        matches = {}
        original = question if original_question is None else original_question
        for phrase, definition in self.concepts.items():
            if phrase not in original and phrase not in question:
                continue
            # When the user named a metric, use their wording to decide whether it overrides
            # the default. Otherwise the resolved question can supply context for a follow-up.
            override_text = original if phrase in original else f"{original} {question}"
            if any(
                term.casefold() in override_text.casefold() for term in definition.get("unless_terms", [])
            ):
                continue
            matches[phrase] = definition
        return matches

    def recall(self, question: str, k: int = 20, *, concepts: dict | None = None) -> list[dict]:
        """Rank actual columns; business aliases outrank generic table/amount words."""
        query, _ = expanded_query(question)
        if concepts is None:
            concepts = self.matching_concepts(question)
        documents = []
        for table in self.tables.values():
            for column in table.columns:
                aliases = column.get("aliases", [])
                column_tokens = schema_terms(
                    column["name"] + " " + column.get("description", "") + " " + " ".join(aliases)
                )
                table_tokens = schema_terms(table.name + " " + table.description)
                documents.append((table, column, aliases, column_tokens, table_tokens))
        frequencies = Counter(t for _, _, _, col, tab in documents for t in col | tab)
        ranked = []
        for table, column, aliases, column_tokens, table_tokens in documents:
            direct = query & column_tokens
            context = query & table_tokens

            def weight(token):
                return math.log(1 + len(documents) / (1 + frequencies[token])) * (
                    2 if len(token) > 1 else 0.3
                )

            # Table context informs ranking, but cannot make every unrelated column look equally relevant.
            score = 3 * sum(weight(t) for t in direct) / math.sqrt(max(1, len(column_tokens)))
            score += 0.2 * sum(weight(t) for t in context) / math.sqrt(max(1, len(table_tokens)))
            exact_aliases = [alias for alias in aliases if alias and alias in question]
            score += 8 * len(exact_aliases)
            for definition in concepts.values():
                if f"{table.name}.{column['name']}" in definition.get("columns", []):
                    score += 12
            ranked.append(
                {
                    "column": f"{table.name}.{column['name']}",
                    "table": table.name,
                    "type": column["type"],
                    "description": column.get("description", ""),
                    "score": round(score, 4),
                    "matched_terms": sorted(direct | context),
                    "exact_aliases": exact_aliases,
                }
            )
        return sorted(ranked, key=lambda item: (-item["score"], item["column"]))[:k]

    def fallback_tables(self, recalled, max_columns=400):
        ordered = list(dict.fromkeys([item["table"] for item in recalled] + sorted(self.tables)))
        selected, used = [], 0
        for name in ordered:
            size = len(self.tables[name].columns)
            if used + size <= max_columns:
                selected.append(name)
                used += size
        return selected

    def expand(self, selected: list[str]) -> list[str]:
        graph = {name: set() for name in self.tables}
        for name, table in self.tables.items():
            for fk in table.foreign_keys:
                graph[name].add(fk["table"])
                graph[fk["table"]].add(name)
        result = set(selected)
        for start in selected:
            queue, seen = deque([(start, [start])]), {start}
            while queue:
                node, path = queue.popleft()
                if node in selected:
                    result.update(path)
                for other in sorted(graph[node] - seen):
                    seen.add(other)
                    queue.append((other, path + [other]))
        return sorted(result)

    def render(self, names):
        return [asdict(self.tables[name]) for name in names]
