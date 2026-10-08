from unittest.mock import Mock

import pytest

from app import graph_cli


@pytest.mark.parametrize(
    ("flags", "expected"),
    [([], True), (["--include-winners"], True), (["--exclude-winners"], False)],
)
@pytest.mark.parametrize(
    ("scene", "identity"),
    [("buyer_bidders", "--buyer-id"), ("supplier_co_bidders", "--supplier-id")],
)
def test_scene_cli_passes_participant_policy_to_query(
    monkeypatch, capsys, flags, expected, scene, identity,
):
    driver = Mock()
    create_driver = Mock(return_value=driver)
    query = Mock(return_value={"include_winners": expected})
    monkeypatch.setattr(graph_cli, "create_driver", create_driver)
    monkeypatch.setattr(graph_cli, "query_neo4j", query)

    assert graph_cli.main([
        "--dataset", "cli-test", "scene", scene, identity, "7", *flags,
    ]) == 0

    create_driver.assert_called_once()
    assert query.call_count == 1
    assert query.call_args.args == (driver, scene)
    assert query.call_args.kwargs["dataset"] == "cli-test"
    assert query.call_args.kwargs["include_winners"] is expected
    driver.close.assert_called_once()
    assert '"include_winners"' in capsys.readouterr().out


def test_scene_cli_rejects_conflicting_participant_flags_before_connecting(monkeypatch):
    create_driver = Mock()
    query = Mock()
    monkeypatch.setattr(graph_cli, "create_driver", create_driver)
    monkeypatch.setattr(graph_cli, "query_neo4j", query)

    with pytest.raises(SystemExit) as raised:
        graph_cli.main([
            "scene", "buyer_bidders", "--buyer-id", "7",
            "--include-winners", "--exclude-winners",
        ])

    assert raised.value.code == 2
    create_driver.assert_not_called()
    query.assert_not_called()
