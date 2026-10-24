"""Run every workbook query against a live Log Analytics workspace (needs `az login`).

    uv run python deploy/agent-gateway/infra/workbooks/validate.py <workspace-id> \
        [--param Session=<id>] [--param RunId=<id>] [--param Search=CVE-2024-3400]

Workbook parameters are substituted the way the portal does ({TimeRange} -> a time
filter, {TimeRange:grain} -> a bin size, text parameters -> their values). A query that
fails to parse or run is reported with the error, so broken KQL is caught before deploy.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def substitute(kql: str, values: dict[str, str]) -> str:
    kql = kql.replace("{TimeRange:grain}", "1h").replace("{TimeRange}", "> ago(1d)")
    for name, value in values.items():
        kql = kql.replace("{" + name + "}", value)
    return kql


def run(workspace: str, kql: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [
            "az",
            "monitor",
            "log-analytics",
            "query",
            "-w",
            workspace,
            "--analytics-query",
            kql,
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        lines = [ln for ln in proc.stderr.splitlines() if "ERROR" in ln or "message" in ln.lower()]
        return False, (lines[-1] if lines else proc.stderr.strip()[-400:])
    return True, f"{len(json.loads(proc.stdout or '[]'))} rows"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("workspace", help="Log Analytics workspace (customer) id")
    ap.add_argument(
        "--param", action="append", default=[], help="Name=value for a workbook parameter"
    )
    args = ap.parse_args(argv)
    values = {
        "Search": "",
        "User": "",
        "Agent": "",
        "Session": "",
        "RunId": "",
        "PriceInput": "0.25",
        "PriceCached": "0.025",
        "PriceOutput": "2.00",
    }
    values.update(dict(p.split("=", 1) for p in args.param))

    failures = 0
    for path in sorted(HERE.glob("*.json")):
        for item in json.loads(path.read_text())["items"]:
            if item["type"] != 3:
                continue
            ok, detail = run(args.workspace, substitute(item["content"]["query"], values))
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {path.stem}/{item['name']}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
