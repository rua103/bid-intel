import io
import zipfile

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.storage import count_notices


def _zip_bytes(entries: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _notice(project: str, product: str) -> str:
    return f"""<html><body><p>项目名称：{project}</p><table>
      <tr><th>品目名称</th><th>采购标的</th><th>品牌</th><th>规格型号</th>
      <th>数量</th><th>单价</th><th>总价</th></tr>
      <tr><td>办公设备</td><td>{product}</td><td>演示品牌</td><td>DEMO-1</td>
      <td>1台</td><td>100</td><td>100</td></tr>
    </table></body></html>"""


def test_batch_upload_groups_nested_attachments_and_reports_orphans(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "batch-api.db"))
    monkeypatch.setattr(settings, "model_base_url", "")
    monkeypatch.setattr(settings, "model_api_key", "")
    monkeypatch.setattr(settings, "model_name", "")

    nested_attachment = _zip_bytes({"technical-details.txt": "项目甲附件内容"})
    matching_attachment = _zip_bytes({"nested.zip": nested_attachment})
    batch_archive = _zip_bytes(
        {
            "notice-1.html": _notice("项目甲", "打印机"),
            "notice-1附件.zip": matching_attachment,
            "notice-2.html": _notice("项目乙", "扫描仪"),
            "unmatched.txt": "未匹配的文件",
        }
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/notices/import-batch",
            files=[("files", ("dataset.zip", batch_archive, "application/zip"))],
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["notices_found"] == 2
        assert payload["notices_imported"] == 2
        assert payload["total_items_found"] == 2
        assert payload["total_participants_found"] == 0
        assert payload["errors"] == []
        assert isinstance(payload["elapsed_seconds"], (int, float))
        assert payload["elapsed_seconds"] >= 0
        assert payload["orphan_files"] == ["dataset.zip!/unmatched.txt"]

        summaries = {
            next(name for name in row["source_files"] if name.endswith(".html")): row
            for row in payload["notices"]
        }
        first = summaries["dataset.zip!/notice-1.html"]
        second = summaries["dataset.zip!/notice-2.html"]
        assert first["notice_id"] != second["notice_id"]
        assert first["items_found"] == second["items_found"] == 1
        assert first["participants_found"] == second["participants_found"] == 0
        assert first["source_files"] == [
            "dataset.zip!/notice-1.html",
            "dataset.zip!/notice-1附件.zip!/nested.zip!/technical-details.txt",
        ]
        assert second["source_files"] == ["dataset.zip!/notice-2.html"]
        assert count_notices(settings.resolved_database_path) == 2

        first_items = client.get("/api/v1/items", params={"query": "打印机"})
        second_items = client.get("/api/v1/items", params={"query": "扫描仪"})
        assert first_items.status_code == second_items.status_code == 200
        assert {item["notice_id"] for item in first_items.json()} == {first["notice_id"]}
        assert {item["notice_id"] for item in second_items.json()} == {second["notice_id"]}
