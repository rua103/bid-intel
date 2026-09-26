"""Create a clearly synthetic, model-free dataset for an offline product demo."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID

from app.config import settings
from app.schemas import ImportResult, ItemCandidate, NoticeMetadata, ParticipantCandidate
from app.storage import connect, initialize, save_import

DEMO_DATASET_ID = UUID("00000000-0000-4000-8000-000000000001").hex


def _notice(
    project_number: str,
    project_name: str,
    buyer: str,
    supplier_rows: list[tuple[str, str, str | None]],
    item_name: str,
    amount: str,
) -> ImportResult:
    filename = f"synthetic_{project_number.lower()}.html"
    amount_value = Decimal(amount)
    participants = [
        ParticipantCandidate(
            package_code="1",
            organization_name=name,
            outcome=outcome,
            award_amount=Decimal(award) if award else None,
            source_file=filename,
            source_location=f"演示样例投标表/{index}",
            source_evidence=f"虚构投标记录：{name}，结果为{outcome}",
            extraction_method="synthetic_demo_fixture",
            confidence=1,
        )
        for index, (name, outcome, award) in enumerate(supplier_rows, start=1)
    ]
    return ImportResult(
        notice_id=0,
        source_files=[filename],
        items_found=1,
        items=[ItemCandidate(
            package_code="1",
            product_name=item_name,
            category="办公设备",
            brand="演示品牌",
            model=f"DEMO-{project_number[-2:]}",
            quantity=Decimal(1),
            quantity_unit="批",
            unit_price=amount_value,
            total_price=amount_value,
            source_file=filename,
            source_location="虚构标的表/1",
            source_evidence="演示用虚构来源，仅用于离线 UI 演示",
            extraction_method="synthetic_demo_fixture",
            confidence=1,
        )],
        metadata=NoticeMetadata(
            project_name=project_name,
            project_number=project_number,
            procurement_unit=buyer,
            project_budget=amount_value * 2,
            announced_total_award=amount_value,
        ),
        participants=participants,
        warnings=["完全虚构的离线演示快照；不得用于准确率或比赛评分"],
    )


def seed_offline_demo(database_root: Path | None = None) -> Path:
    root = database_root or settings.resolved_database_path.parent
    directory = root / "datasets"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{DEMO_DATASET_ID}.sqlite"
    if path.exists():
        path.unlink()
    initialize(path)
    with connect(path) as connection:
        connection.execute("CREATE TABLE dataset_info (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO dataset_info(id, name) VALUES (1, ?)",
            ("离线演示样例（纯虚构）",),
        )

    records = [
        _notice(
            "DEMO-2026-001",
            "办公打印设备采购（虚构）",
            "示例市公共服务中心（虚构）",
            [
                ("示例打印科技有限公司（虚构）", "winner", "18000"),
                ("示例文印设备有限公司（虚构）", "nonwinner", None),
                ("示例办公供应有限公司（虚构）", "nonwinner", None),
            ],
            "激光打印机",
            "18000",
        ),
        _notice(
            "DEMO-2026-002",
            "办公电脑采购（虚构）",
            "示例市公共服务中心（虚构）",
            [
                ("示例文印设备有限公司（虚构）", "winner", "42000"),
                ("示例打印科技有限公司（虚构）", "nonwinner", None),
            ],
            "台式计算机",
            "42000",
        ),
        _notice(
            "DEMO-2026-003",
            "社区服务设备采购（虚构）",
            "虚构新区社区服务中心",
            [
                ("示例打印科技有限公司（虚构）", "winner", "9000"),
                ("示例办公供应有限公司（虚构）", "nonwinner", None),
            ],
            "多功能一体机",
            "9000",
        ),
    ]
    for index, record in enumerate(records, start=1):
        save_import(path, record, source_key=f"synthetic-offline-demo-{index}")
    return path


def main() -> None:
    path = seed_offline_demo()
    print(f"已重建虚构离线演示数据集 {DEMO_DATASET_ID}：{path}")


if __name__ == "__main__":
    main()
