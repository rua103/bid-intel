import json

from fastapi.testclient import TestClient

from app.main import app

HTML_NOTICE = """<html><body>
  <table>
    <tr><th>品目名称</th><th>采购标的</th><th>品牌</th><th>规格型号</th>
      <th>数量</th><th>单价(元)</th><th>总价(元)</th></tr>
    <tr><td>办公设备</td><td>激光打印机</td><td>品牌甲</td><td>P-1</td>
      <td>2台</td><td>1800</td><td>3600</td></tr>
  </table>
</body></html>"""


def uploaded_json(value: dict, filename: str) -> tuple[str, bytes, str]:
    return filename, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json"


def test_web_draft_uses_blank_gold_and_run_rejects_self_matching_copy():
    with TestClient(app) as client:
        draft_response = client.post(
            "/api/v1/evaluation/draft?mode=rules",
            files=[("files", ("notice.html", HTML_NOTICE.encode("utf-8"), "text/html"))],
        )
        assert draft_response.status_code == 200, draft_response.text
        data = draft_response.json()
        gold = data["gold"]
        predictions = data["predictions"]
        assert predictions["notices"][0]["packages"][0]["items"]
        gold_package = gold["notices"][0]["packages"][0]
        assert gold_package["items"] == []
        assert gold_package["buyer"] is None
        assert gold_package["winners"] == []
        assert gold_package["bidders"] == []

        # A user can no longer produce a report by only checking the reviewed box.
        gold["status"] = "reviewed"
        empty_gold_response = client.post(
            "/api/v1/evaluation/run",
            files={
                "gold": uploaded_json(gold, "gold.json"),
                "predictions": uploaded_json(predictions, "predictions.json"),
            },
        )
        assert empty_gold_response.status_code == 422
        assert "没有人工标注记录" in empty_gold_response.json()["detail"]

        # Importing the prediction file as reviewed gold is rejected even when the
        # uploads have different filenames and independently serialized JSON.
        copied_gold = json.loads(json.dumps(predictions, ensure_ascii=False))
        copied_gold["status"] = "reviewed"
        copied_gold["notices"][0]["packages"][0]["items"][0]["item_id"] = "manually-renamed"
        copied_gold["notices"][0]["packages"][0]["items"][0]["quantity"] = "2.000"
        copied_response = client.post(
            "/api/v1/evaluation/run",
            files={
                "gold": uploaded_json(copied_gold, "gold.json"),
                "predictions": uploaded_json(predictions, "predictions.json"),
            },
        )
        assert copied_response.status_code == 422
        assert "内容完全一致" in copied_response.json()["detail"]

        # A legitimately exact, independently verified gold set remains reportable
        # through an explicit opt-in rather than being silently treated as a draft.
        exact_response = client.post(
            "/api/v1/evaluation/run?allow_identical_gold=true",
            files={
                "gold": uploaded_json(copied_gold, "gold.json"),
                "predictions": uploaded_json(predictions, "predictions.json"),
            },
        )
        assert exact_response.status_code == 200, exact_response.text
        assert exact_response.json()["report"]["field_micro"]["accuracy"] == 1
        assert "显式声明独立核验" in exact_response.json()["markdown"]

        # An independently edited gold set still runs and reports the discrepancy.
        copied_gold["notices"][0]["packages"][0]["items"][0]["brand"] = "经人工核验品牌"
        evaluated = client.post(
            "/api/v1/evaluation/run",
            files={
                "gold": uploaded_json(copied_gold, "gold.json"),
                "predictions": uploaded_json(predictions, "predictions.json"),
            },
        )
        assert evaluated.status_code == 200, evaluated.text
        assert evaluated.json()["report"]["field_micro"]["accuracy"] < 1
