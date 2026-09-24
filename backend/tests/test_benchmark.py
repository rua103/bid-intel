from app.benchmark import markdown_report, percentile, run_benchmark


def test_synthetic_benchmark_uses_real_rules_and_persistence_without_model(monkeypatch):
    def forbidden(*_, **__):
        raise AssertionError("synthetic benchmark must never call a model")

    monkeypatch.setattr("app.ingestion.extract_unstructured_items", forbidden)
    # Even an API-configured environment must not enable model traffic.
    monkeypatch.setenv("MODEL_API_KEY", "test-never-send")
    monkeypatch.setenv("MODEL_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("MODEL_NAME", "deepseek-chat")
    report = run_benchmark(notices=4, repeats=2)
    counts = report["data"]["counts"]
    assert counts["notices"] == counts["projects"] == counts["packages"] == 4
    assert counts["procurement_items"] == report["data"]["expected_items"] == 8
    assert counts["bid_participations"] == 12
    assert counts["awards"] == 4
    assert report["data"]["extracted_items"] == 8
    assert set(report["queries"]) == {"buyer_awardees", "buyer_bidders", "supplier_co_bidders", "common_buyers", "common_projects"}
    for measurement in report["queries"].values():
        assert measurement["result_rows"] > 0
        assert measurement["first_call_ms"] >= 0
        assert measurement["warm_p95_ms"] >= measurement["warm_p50_ms"]
        assert len(measurement["warm_samples_ms"]) == 2
    assert "投标主体由测试生成器注入" in markdown_report(report)
    assert "不含 HTTP" in markdown_report(report)


def test_percentile_uses_nearest_rank():
    assert percentile(list(range(1, 101)), .95) == 95
