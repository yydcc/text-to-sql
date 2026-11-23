"""Incremental, per-question records. No credentials or database connection strings."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

STAGES = {1: "question_polish", 2: "column_recall", 3: "sql_generation", 4: "execution_repair"}


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


class RunRecord:
    def __init__(self, root, question, history, config, secrets=()):
        self.run_id = (
            datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8]
        )
        self.directory = Path(root).expanduser().resolve() / self.run_id
        self.directory.mkdir(parents=True, mode=0o700)
        self.secrets = [value for value in secrets if value]
        self.stages = {
            n: {"step": n, "name": name, "status": "pending", "events": []} for n, name in STAGES.items()
        }
        self._stage_started = {}
        self.calls = 0
        self.current_step = 1
        self.manifest = {
            "run_id": self.run_id,
            "started_at": now(),
            "status": "running",
            "question": question,
            "history": history,
            "config": config,
        }
        self.write("run.json", self.manifest)
        for n in STAGES:
            self.write_stage(n)

    def clean(self, value):
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        # Escape-aware redaction also works if a secret occurs inside exception text.
        for secret in self.secrets:
            encoded = json.dumps(secret, ensure_ascii=False)[1:-1]
            text = text.replace(encoded, "[REDACTED]")
        return json.loads(text)

    def write(self, name, value):
        target = self.directory / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(
            json.dumps(self.clean(value), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        os.chmod(temp, 0o600)
        temp.replace(target)

    def write_stage(self, step):
        self.write(f"{step:02d}_{STAGES[step]}.json", self.stages[step])

    def start(self, step, inputs):
        self.current_step = step
        self._stage_started[step] = time.monotonic()
        self.manifest.update(current_step=step)
        self.write("run.json", self.manifest)
        self.stages[step].update(status="running", started_at=now(), input=inputs)
        self.write_stage(step)

    def event(self, event):
        step = event["step"]
        self.current_step = step
        event = {"timestamp": now(), **event}
        self.stages[step]["events"].append(event)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.clean(event), ensure_ascii=False, default=str) + "\n")
        os.chmod(self.directory / "events.jsonl", 0o600)
        self.write_stage(step)

    def complete(self, step, status="ok"):
        self.stages[step].update(
            status=status,
            finished_at=now(),
            elapsed_ms=round((time.monotonic() - self._stage_started[step]) * 1000, 3),
        )
        self.write_stage(step)

    def start_call(self, task, payload, output_schema):
        self.calls += 1
        name = f"model_calls/{self.calls:02d}_step_{self.current_step}.json"
        item = {
            "step": self.current_step,
            "task": task,
            "input": payload,
            "output_schema": output_schema,
            "started_at": now(),
            "status": "running",
        }
        self.write(name, item)
        return name, item

    def finish(self, result):
        for step, stage in self.stages.items():
            if stage["status"] == "pending":
                stage.update(
                    status="skipped", finished_at=now(), reason=f"Pipeline ended with {result['status']}"
                )
            elif stage["status"] == "running":
                stage.update(
                    status="error" if result["status"] == "error" else result["status"], finished_at=now()
                )
            if stage["status"] != "skipped" and step in self._stage_started and "elapsed_ms" not in stage:
                stage["elapsed_ms"] = round((time.monotonic() - self._stage_started[step]) * 1000, 3)
            self.write_stage(step)
        self.manifest.update(
            status=result["status"],
            finished_at=now(),
            stages={str(n): stage["status"] for n, stage in self.stages.items()},
        )
        self.write("run.json", self.manifest)
        self.write("result.json", result)
