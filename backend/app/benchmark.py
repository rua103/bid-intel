"""Reproducible synthetic pipeline benchmark. Never calls the configured model.

HTML item tables and metadata pass through the actual extraction/persistence
code. Bidder fixtures are injected explicitly, so query timing is useful while
the report never presents synthetic fixtures as model extraction quality.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import analytics
from app.config import Settings
from app.ingestion import extract_notice
from app.parsers import SourceDocument
from app.schemas import ParticipantCandidate
from app.storage import connect, save_import


def synthetic_notice(index: int, *, buyers: int = 20) -> SourceDocument:
    """Two line items and an explicit bidder table in a completely fictional HTML."""
    winner = index % 2
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head><body>
<h1>合成基准数据，不是真实采购公告</h1>
<p>项目名称：合成采购项目{index:05d}</p>
<p>项目编号：SYN-{index:05d}</p><p>采购单位：合成采购单位{index % buyers:02d}</p>
<p>预算金额：200.03元</p><p>总中标/成交金额：180.03元</p>
<table><tr><th>采购标的</th><th>品目</th><th>品牌</th><th>规格型号</th><th>数量</th><th>单价</th><th>总价</th></tr>
<tr><td>合成设备A</td><td>办公设备</td><td>虚构品牌甲</td><td>SYN-A</td><td>1台</td><td>100.01元</td><td>100.01元</td></tr>
<tr><td>合成设备B</td><td>办公设备</td><td>虚构品牌乙</td><td>SYN-B</td><td>1台</td><td>80.02元</td><td>80.02元</td></tr></table>
<table><tr><th>投标主体</th><th>结果</th></tr>
<tr><td>合成供应商00</td><td>{'中标' if winner == 0 else '未中标'}</td></tr>
<tr><td>合成供应商01</td><td>{'中标' if winner == 1 else '未中标'}</td></tr>
<tr><td>合成供应商{2 + index % 38:02d}</td><td>未中标</td></tr></table>
</body></html>"""
    return SourceDocument(filename=f"synthetic_{index:05d}.html", content=html.encode("utf-8"))


def synthetic_participants(index: int, filename: str) -> list[ParticipantCandidate]:
    winner = index % 2
    return [ParticipantCandidate(
        organization_name=f"合成供应商{supplier:02d}",
        outcome="winner" if supplier == winner else "nonwinner",
        award_amount=Decimal("180.03") if supplier == winner else None,
        source_file=filename, source_location=f"synthetic-ground-truth:bidder:{supplier}",
        source_evidence="由合成生成器明确注入的投标主体，不是自动抽取结果",
        extraction_method="synthetic_ground_truth_fixture", confidence=1,
    ) for supplier in (0, 1, 2 + index % 38)]


def percentile(values: list[float], quantile: float) -> float:
    """Nearest-rank percentile, specified in the report for reproducibility."""
    if not values:
        raise ValueError("percentile requires at least one observation")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _query_measurement(function, repeats: int) -> dict[str, Any]:
    first_started = time.perf_counter()
    result = function()
    first_ms = (time.perf_counter() - first_started) * 1000
    durations = []
    for _ in range(repeats):
        started = time.perf_counter()
        function()
        durations.append((time.perf_counter() - started) * 1000)
    return {
        "first_call_ms": round(first_ms, 4),
        "warm_p50_ms": round(statistics.median(durations), 4),
        "warm_p95_ms": round(percentile(durations, .95), 4),
        "warm_min_ms": round(min(durations), 4),
        "warm_max_ms": round(max(durations), 4),
        "warm_samples_ms": [round(value, 4) for value in durations],
        "result_rows": len(result.get("awardees", result.get("top_bidders", result.get("top_co_bidders", result.get("buyers", result.get("packages", [])))))),
    }


def run_benchmark(*, notices: int = 1000, repeats: int = 30) -> dict[str, Any]:
    if notices < 2 or repeats < 1:
        raise ValueError("notices must be >= 2 and repeats must be >= 1")
    # Explicit blanks + rules mode block environment/file-configured API keys.
    offline_settings = Settings(model_base_url="", model_api_key="", model_name="", _env_file=None)
    if hasattr(offline_settings, "ocr_enabled"):
        offline_settings = offline_settings.model_copy(update={"ocr_enabled": False})
    generation_started = time.perf_counter()
    # Odd buyer cardinality ensures each buyer receives both alternating winners.
    buyers = min(19, max(1, notices // 2))
    if buyers % 2 == 0:
        buyers -= 1
    documents = [synthetic_notice(i, buyers=buyers) for i in range(notices)]
    generation_seconds = time.perf_counter() - generation_started
    extraction_seconds = fixture_seconds = persistence_seconds = 0.0
    extraction_items = 0
    with tempfile.TemporaryDirectory(prefix="bid-intel-synthetic-") as directory:
        database = Path(directory) / "synthetic.db"
        started = time.perf_counter()
        for index, document in enumerate(documents):
            stage = time.perf_counter()
            result = extract_notice([document], extraction_mode="rules", model_settings=offline_settings)
            extraction_seconds += time.perf_counter() - stage
            extraction_items += result.items_found
            stage = time.perf_counter()
            # The rule baseline intentionally does not guess bidder identities.
            result = result.model_copy(update={"participants": synthetic_participants(index, document.filename)})
            fixture_seconds += time.perf_counter() - stage
            stage = time.perf_counter()
            save_import(database, result)
            persistence_seconds += time.perf_counter() - stage
        pipeline_seconds = time.perf_counter() - started
        with connect(database) as db:
            counts = {table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                      for table in ("notices", "projects", "packages", "organizations", "procurement_items", "bid_participations", "awards")}
            ids = {row["canonical_name"]: int(row["id"]) for row in db.execute("SELECT id,canonical_name FROM organizations")}
        db.close()
        buyer = ids["合成采购单位00"]
        supplier = ids["合成供应商00"]
        selected = [supplier, ids["合成供应商01"]]
        queries = {
            "buyer_awardees": lambda: analytics.buyer_awardees(database, buyer),
            "buyer_bidders": lambda: analytics.buyer_bidders(database, buyer),
            "supplier_co_bidders": lambda: analytics.supplier_co_bidders(database, supplier),
            "common_buyers": lambda: analytics.common_award_buyers(database, selected),
            "common_projects": lambda: analytics.common_bid_packages(database, selected),
        }
        measurements = {name: _query_measurement(fn, repeats) for name, fn in queries.items()}
        database_bytes = database.stat().st_size

    return {
        "benchmark_type": "synthetic_rules_pipeline_and_sqlite_queries",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "notice_count": notices, "query_warm_repeats": repeats,
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "processor": platform.processor(), "sqlite": __import__("sqlite3").sqlite_version},
        "data": {"html_documents": notices, "attachments": 0, "bytes": sum(len(doc.content) for doc in documents),
                 "database_bytes": database_bytes, "counts": counts, "expected_items": notices * 2,
                 "extracted_items": extraction_items, "buyer_count": buyers,
                 "participant_source": "generated ground truth explicitly injected before persistence"},
        "pipeline_seconds": {"html_generation_excluded": round(generation_seconds, 6),
                             "parse_and_rule_extraction": round(extraction_seconds, 6),
                             "participant_fixture_injection": round(fixture_seconds, 6),
                             "persistence": round(persistence_seconds, 6),
                             "end_to_end_including_fixture_injection": round(pipeline_seconds, 6)},
        "mean_notice_ms_including_fixture_injection": round(pipeline_seconds * 1000 / notices, 4),
        "queries": measurements,
        "limitations": [
            "合成 HTML 数据和规则基线；不是官方数据，也不是 Qwen/DeepSeek 模型性能或准确率测量。",
            "端到端区间为已有 HTML 字节进入解析、规则提取、测试投标主体注入、SQLite 持久化完成；不含文件生成。",
            "投标主体由生成器注入，仅用于五类关系查询负载，不计为自动实体识别成果。",
            "first_call 是该场景本进程首次查询，不清空操作系统磁盘缓存；warm 是随后重复执行。",
            "查询计时仅覆盖 Python SQLite 分析函数，含建连/初始化及结果组装，不含 HTTP、网络或前端渲染；不等同竞赛响应时间。",
            "所有公告采用相同简单表格结构、每公告两标的一包三投标主体，无附件/OCR/模型请求。",
            "p50 采用中位数；p95 采用 nearest-rank；五个场景按固定次序测量。",
        ],
    }


def markdown_report(report: dict[str, Any]) -> str:
    pipeline = report["pipeline_seconds"]
    lines = [
        "# 合成数据基准报告", "",
        "**非官方成绩；无模型调用；投标主体由测试生成器注入。**", "",
        f"生成时间：{report['created_at_utc']}；公告数：{report['notice_count']}；每场景 warm 重复：{report['query_warm_repeats']}。", "",
        f"Python {report['environment']['python']}；{report['environment']['platform']}；SQLite {report['environment']['sqlite']}。", "",
        "| 导入阶段 | 秒 |", "| --- | ---: |",
        f"| 解析与规则字段提取 | {pipeline['parse_and_rule_extraction']} |",
        f"| 测试投标主体注入 | {pipeline['participant_fixture_injection']} |",
        f"| SQLite 入库 | {pipeline['persistence']} |",
        f"| 端到端（含测试主体注入） | {pipeline['end_to_end_including_fixture_injection']} |", "",
        f"平均每公告：{report['mean_notice_ms_including_fixture_injection']} ms。实际标的数：{report['data']['extracted_items']}；预期：{report['data']['expected_items']}。", "",
        "| 场景 | 首次调用 ms | warm p50 ms | warm p95 ms | 返回主行数 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, values in report["queries"].items():
        lines.append(f"| {name} | {values['first_call_ms']} | {values['warm_p50_ms']} | {values['warm_p95_ms']} | {values['result_rows']} |")
    lines.extend(["", "统计边界：", ""] + [f"- {text}" for text in report["limitations"]])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an isolated, offline synthetic benchmark")
    parser.add_argument("--notices", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path("../docs/benchmarks/synthetic-1000.json"))
    args = parser.parse_args(argv)
    report = run_benchmark(notices=args.notices, repeats=args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
    print(f"Synthetic benchmark saved: {args.output.resolve()}")
    print(f"Notices: {report['notice_count']}; mean: {report['mean_notice_ms_including_fixture_injection']} ms/notice")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
