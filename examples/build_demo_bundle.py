"""Rebuild the synthetic attachment ZIP paired with demo_notice.html."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "demo_notice.zip"


def main() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "投标评审结果"
    sheet.append(["采购包编号", "投标人名称", "是否中标", "中标金额（元）", "综合得分"])
    sheet.append(["1", "示例打印科技有限公司（虚构）", "中标", 18000, 93.47])
    sheet.append(["1", "示例文印设备有限公司（虚构）", "未中标", None, 88.10])
    sheet.append(["1", "示例办公供应有限公司（虚构）", "未中标", None, 87.00])
    workbook_bytes = BytesIO()
    workbook.save(workbook_bytes)
    with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("评审结果（虚构）.xlsx", workbook_bytes.getvalue())
    print(f"已生成虚构演示附件：{OUTPUT}")


if __name__ == "__main__":
    main()
