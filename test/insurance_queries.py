"""Run the ten insurance questions against the configured PostgreSQL database."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from text_to_sql.agent import Agent
from text_to_sql.cli import settings
from text_to_sql.schema import Catalog

QUESTIONS = (
    "查询每种保险类型的保险金额的平均值、最大值和最小值。",
    "查询每个客户购买的保单数以及这些保单的总保费金额。",
    "查询每个受益人的平均受益比例，并找出受益比例最高和最低的受益人。",
    "查询每个投保日期月份的新保单数量的趋势，以便分析哪个月份有最多的新保单。",
    "查询每个客户的保单数以及这些保单的保费支付状态（PremiumPaymentStatus）的分布情况，以便了解客户的支付行为。",
    "查询保险期限在一年以内的保单数量，以及它们的平均保费金额，用于分析短期保险的情况。",
    "查询每个投保人的平均保费金额，并找出支付最高和最低保费的投保人。",
    "查询每个附加险种的保单数量和总保费金额，以便了解不同附加险种的市场份额。",
    "查询每个代理人代理的保单数量和总保费金额，以评估代理人的业绩。",
    "查询保单状态随时间的变化趋势，以便分析生效、终止和暂停保单的变化情况。",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for outputs and four-stage traces.")
    parser.add_argument("--cases", nargs="+", type=int, choices=range(1, 11), default=range(1, 11))
    args = parser.parse_args()

    config = settings()
    catalog = Catalog.load(**config)
    root = args.output.resolve()
    root.mkdir(parents=True, mode=0o700)

    def run(case):
        question = QUESTIONS[case - 1]
        agent = Agent(config["dsn"], catalog=catalog, log_dir=root / "runs", max_rows=20_000)
        result = agent.ask(question)
        result.update(case=case, question=question)
        (root / f"{case:02d}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"{case:02d} {result['status']} {result.get('answer') or result.get('error', '')}", flush=True)
        return bool(result.get("sql") and result.get("result") and not result["result"]["truncated"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(run, dict.fromkeys(args.cases)))
    print(root, flush=True)
    return 0 if all(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
