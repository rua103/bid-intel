"""GAP 6b: measure five display queries on a snapshot, never infer correctness."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import analytics

TABLES = ("notices", "projects", "packages", "organizations", "procurement_items",
          "bid_participations", "awards", "import_receipts")
LIMITATIONS = [
    "本报告只验证返回结果、条数与耗时；1038 条回灌数据没有 gold，不能证明答案正确。",
    "所选主体已知有关系，用于非空演示；不是所有主体/主体组合的覆盖率或准确率测量。",
    "源库以 mode=ro 打开，经 SQLite backup 建临时副本；分析函数在副本执行，未注入主体或修正抽取数据。",
    "函数计时包含建连、initialize 和结果组装，不含备份、选参、HTTP、网络或浏览器渲染。",
    "首调为该进程该场景首次函数调用，未清理 OS 缓存；随后 30 次 warm，p50 为中位数，p95 为 nearest-rank。",
    "HTTP 实测若启用，单次耗时含本机 HTTP、鉴权和 JSON 传输；不代表浏览器渲染或多用户负载。",
    "场景 2/3 的 include_winners=false 当前执行 outcome != 'winner'，仍含 unknown，不能等同确定未中标。",
    "频次沿用当前实现按采购包统计；场景 5 同时报告去重项目数与包数，金额未按原件核验。",
    "未验证 Neo4j、独立 gold 对照（6a）或第二台设备；未调用模型、未重新抽取公告。",
]


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def counts(result):
    return {**{key: len(value) for key, value in result.items() if isinstance(value, list)},
            **{key: result[key] for key in ("package_count", "project_count") if key in result}}


def measure(fn, repeats):
    started = time.perf_counter()
    result = fn()
    first_ms = (time.perf_counter() - started) * 1000
    durations = []
    for _ in range(repeats):
        started = time.perf_counter()
        repeated = fn()
        durations.append((time.perf_counter() - started) * 1000)
        if repeated != result:
            raise RuntimeError("Repeated query returned different results")
    return {
        "first_call_ms": round(first_ms, 4),
        "warm_p50_ms": round(statistics.median(durations), 4),
        "warm_p95_ms": round(sorted(durations)[math.ceil(.95 * repeats) - 1], 4),
        "warm_samples_ms": [round(value, 4) for value in durations],
        "repeat_results_identical": True, "result_counts": counts(result), "result": result,
    }


def http_smoke(base, dataset_id, queries, env_file):
    import httpx
    from app.config import Settings

    if urlparse(base).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("HTTP check only supports the local demo service")
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        session = client.get("/api/v1/auth/me")
        session.raise_for_status()
        if not session.json()["authenticated"]:
            config = Settings(_env_file=env_file)
            login = client.post("/api/v1/auth/login", json={
                "username": config.auth_username, "password": config.auth_password,
            })
            login.raise_for_status()
        headers = {"X-Dataset-ID": dataset_id}
        health = client.get("/api/v1/health", headers=headers)
        health.raise_for_status()
        for query in queries.values():
            request = query["request"]
            kwargs = {"params": request.get("params", {}), "headers": headers}
            if "json" in request:
                kwargs["json"] = request["json"]
            started = time.perf_counter()
            response = client.request(request["method"], request["path"], **kwargs)
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            payload = response.json()
            query["http"] = {"status": response.status_code, "elapsed_ms": round(elapsed_ms, 4),
                             "result_counts": counts(payload), "result": payload,
                             "matches_function_result": payload == query["result"],
                             "differing_keys": sorted(key for key in set(payload) | set(query["result"])
                                                      if payload.get(key) != query["result"].get(key)),
                             "display_arrays_match": all(payload.get(key) == value for key, value
                                                         in query["result"].items() if isinstance(value, list))}
        return {"base_url": base, "health": health.json(), "authentication": "normal login",
                "server_code_revision": "unknown; existing running service, not restarted",
                "X-Dataset-ID": dataset_id, "browser_rendering_checked": False}


def markdown(report):
    data = report["dataset"]
    lines = ["# GAP 6b：1038 条回灌数据五类查询演示实测", "",
             "**只验证是否返回可展示结果、条数与耗时，不能证明答案正确。**", "",
             f"实测时间（UTC）：`{report['created_at_utc']}`；数据集：`{data['id']}`（{data['name']}）。",
             f"代码基准：`{report['code']['git_head']}`；源库：`{data['path']}`。", "",
             "## 数据与输入", "", "| 表 | 条数 |", "|---|---:|"]
    lines += [f"| `{key}` | {value} |" for key, value in data["counts"].items()]
    lines += ["", "投标状态：" + json.dumps(data["outcomes"], ensure_ascii=False), "",
              "主体选择限定为当前网页下拉前 500 可见且有关系的主体，不代表所有主体的覆盖率：", ""]
    lines += [f"- {role}：" + "；".join(f"`{x['id']}` {x['canonical_name']}" for x in orgs)
              for role, orgs in data["selection"].items()]
    lines += ["", "## 返回结果与计时", "",
              f"每场景 warm 重复 {report['warm_repeats']} 次，全部结果与首调一致。单位：ms。", "",
              "| 场景 | 返回条数 | 首调 | warm p50 | warm p95 | HTTP 单次 |",
              "|---|---|---:|---:|---:|---:|"]
    for name, query in report["queries"].items():
        row_counts = "；".join(f"{k}={v}" for k, v in query["result_counts"].items())
        lines.append(f"| `{name}` | {row_counts} | {query['first_call_ms']} | "
                     f"{query['warm_p50_ms']} | {query['warm_p95_ms']} | "
                     f"{query.get('http', {}).get('elapsed_ms', '未测')} |")
    if report["http"]:
        lines += ["", "HTTP 请求使用 X-Dataset-ID 选择同一原库；已运行服务未重启，代码版本未确认。", ""]
        for name, query in report["queries"].items():
            check = query["http"]
            lines.append(f"- `{name}`：HTTP {check['status']}；结果数组一致={check['display_arrays_match']}；"
                         f"响应差异字段={check['differing_keys']}。")
    lines += ["", "完整响应、全部重复耗时、请求路径与参数见同名 JSON。下列为每类响应的首条主结果：", ""]
    main_keys = ["awardees", "top_bidders", "top_co_bidders", "buyers", "packages"]
    for (name, query), key in zip(report["queries"].items(), main_keys):
        lines += [f"### {name}", "", "```json",
                  json.dumps(query["result"][key][:1], ensure_ascii=False, indent=2), "```", ""]
    lines += ["## 验证边界与复现", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines += ["", f"源库 SHA-256 前后相同：`{data['source_unchanged']}`；`{data['sha256_after']}`。",
              "", ("本次在固定提交的独立 checkout 中测量，避免混入其它任务正在修改的查询口径。"
                     "复现时将本脚本放入同一代码基准的 scripts 目录，并在该 checkout 根目录（PowerShell）运行："), "", "```powershell",
              report["reproduction_command"], "```", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--buyer-id", type=int, default=124)
    parser.add_argument("--supplier-id", type=int, default=114)
    parser.add_argument("--pair", nargs=2, type=int, default=[114, 157])
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-base")
    parser.add_argument("--env-file", type=Path, default=ROOT / "backend/.env")
    args = parser.parse_args()
    if args.repeats < 1 or len(set(args.pair)) != 2:
        parser.error("repeats must be positive and pair must have two distinct IDs")
    source = args.database.resolve(strict=True)
    if source in (args.output.resolve(), args.output.with_suffix('.md').resolve()):
        parser.error("output must not overwrite source")
    before = sha256(source)
    with tempfile.TemporaryDirectory(prefix="gap6b-") as directory:
        snapshot = Path(directory) / "snapshot.sqlite"
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(snapshot)) as target,
        ):
            original.backup(target)
        with closing(sqlite3.connect(snapshot)) as db:
            db.row_factory = sqlite3.Row
            data = {"id": source.stem, "path": str(source),
                    "name": db.execute("SELECT name FROM dataset_info WHERE id=1").fetchone()[0],
                    "counts": {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES},
                    "outcomes": dict(db.execute("SELECT outcome,COUNT(*) FROM bid_participations GROUP BY outcome")),
                    "sha256_before": before}
            data["selection"] = {}
            visible = {r[0] for r in db.execute("SELECT id FROM organizations ORDER BY canonical_name LIMIT 500")}
            for role, ids in {"buyer": [args.buyer_id], "supplier": [args.supplier_id], "pair": args.pair}.items():
                data["selection"][role] = []
                for oid in ids:
                    row = db.execute("SELECT id,canonical_name FROM organizations WHERE id=?", (oid,)).fetchone()
                    if row is None or oid not in visible:
                        raise ValueError(f"Organization {oid} is missing or outside the frontend's first 500")
                    data["selection"][role].append(dict(row))
        b, s, pair = args.buyer_id, args.supplier_id, args.pair
        cases = [
            ("buyer_awardees", lambda: analytics.buyer_awardees(snapshot, b), "GET", f"buyers/{b}/awardees", {}),
            ("buyer_bidders", lambda: analytics.buyer_bidders(snapshot, b, include_winners=False, top=5),
             "GET", f"buyers/{b}/bidders", {"params": {"include_winners": "false", "top": 5}}),
            ("supplier_co_bidders", lambda: analytics.supplier_co_bidders(snapshot, s, include_winners=False, top=5),
             "GET", f"suppliers/{s}/co-bidders", {"params": {"include_winners": "false", "top": 5}}),
            ("common_buyers", lambda: analytics.common_award_buyers(snapshot, pair),
             "POST", "common-buyers", {"json": {"organization_ids": pair}}),
            ("common_projects", lambda: analytics.common_bid_packages(snapshot, pair),
             "POST", "common-projects", {"json": {"organization_ids": pair}}),
        ]
        queries = {name: {"request": {"method": method, "path": "/api/v1/analytics/" + route, **extra},
                          **measure(fn, args.repeats)} for name, fn, method, route, extra in cases}
    http = http_smoke(args.api_base, source.stem, queries, args.env_file) if args.api_base else None
    data["sha256_after"] = sha256(source)
    data["source_unchanged"] = before == data["sha256_after"]
    if not data["source_unchanged"]:
        raise RuntimeError("Source changed during measurement; rerun on a stable dataset")
    command = (f'& "{sys.executable}" scripts/check_analytics_display.py '
               f'--database "{args.database}" --buyer-id {b} --supplier-id {s} '
               f'--pair {pair[0]} {pair[1]} --repeats {args.repeats} --output "{args.output}"')
    if args.api_base:
        command += f' --api-base "{args.api_base}" --env-file "{args.env_file}"'
    report = {"benchmark_type": "official_replayed_dataset_query_display_only",
              "created_at_utc": datetime.now(UTC).isoformat(), "dataset": data,
              "code": {"git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                       "analytics_sha256": sha256(ROOT / "backend/app/analytics.py")},
              "environment": {"python": platform.python_version(), "platform": platform.platform(),
                              "sqlite": sqlite3.sqlite_version},
              "warm_repeats": args.repeats, "queries": queries, "http": http,
              "limitations": [x.replace("30 次 warm", f"{args.repeats} 次 warm") for x in LIMITATIONS],
              "reproduction_command": command}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "source_unchanged": data["source_unchanged"],
                      "queries": {k: {x: v[x] for x in ("result_counts", "first_call_ms", "warm_p50_ms", "warm_p95_ms")}
                                  for k, v in queries.items()}}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
