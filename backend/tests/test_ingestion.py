import io
import zipfile

from app.ingestion import import_batch
from app.parsers import SourceDocument
from app.storage import count_notices, search_items


def _notice(title: str, product: str) -> str:
    return f"""<html><body><p>项目名称：{title}</p><table>
      <tr><th>品目名称</th><th>采购标的</th><th>品牌</th><th>规格型号</th><th>数量</th><th>单价</th><th>总价</th></tr>
      <tr><td>办公设备</td><td>{product}</td><td>演示品牌</td><td>DEMO-1</td><td>1台</td><td>100</td><td>100</td></tr>
    </table></body></html>"""


def test_batch_import_groups_notice_and_matching_attachment_zips(tmp_path):
    attachment = io.BytesIO()
    with zipfile.ZipFile(attachment, "w") as archive:
        archive.writestr("details.txt", "附件补充文本")
    batch = io.BytesIO()
    with zipfile.ZipFile(batch, "w") as archive:
        archive.writestr("notice-1.html", _notice("项目甲", "打印机"))
        archive.writestr("notice-1附件.zip", attachment.getvalue())
        archive.writestr("notice-2.html", _notice("项目乙", "扫描仪"))
        archive.writestr("notice-2附件.zip", attachment.getvalue())

    database = tmp_path / "batch.db"
    result = import_batch([SourceDocument("dataset.zip", batch.getvalue())], database)
    assert result.notices_found == 2
    assert result.notices_imported == 2
    assert result.total_items_found == 2
    assert not result.orphan_files
    assert count_notices(database) == 2
    assert len(search_items(database, query="打印机")) == 1
    assert len(search_items(database, query="扫描仪")) == 1
    assert any("notice-1附件.zip!" in " ".join(row.source_files) for row in result.notices)
