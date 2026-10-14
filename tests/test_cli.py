import json

import pytest

from matic_desk import cli


@pytest.fixture(autouse=True)
def quiet_logging(monkeypatch):
    monkeypatch.setattr(cli, "setup_logging", lambda **kwargs: None)


def parse(*argv: str):
    return cli.build_parser().parse_args(list(argv))


def test_parse_json_flag_either_side():
    assert parse("--json", "bag").json is True
    assert parse("bag", "--json").json is True
    assert parse("status").json is False


def test_parse_clean():
    args = parse("clean", "Kitchen", "Office", "--mode", "vacuum", "--coverage", "heavy_duty", "--ordered")
    assert (args.rooms, args.whole_floor, args.mode, args.coverage, args.ordered) == (
        ["Kitchen", "Office"], False, "vacuum", "heavy_duty", True
    )
    assert parse("clean", "--all").whole_floor is True
    with pytest.raises(SystemExit):
        parse("clean", "Office", "--mode", "steam")


def test_parse_bag_swap_set_map_alias():
    args = parse("bag", "swap", "--at", "2026-10-06", "--note", "new box")
    assert (args.bag_command, args.at, args.note) == ("swap", "2026-10-06", "new box")
    assert parse("bag").bag_command is None
    assert parse("set", "water-flow", "1.2").setting == "water_flow"
    assert parse("history", "-n", "3").limit == 3
    assert parse("map", "--no-route").route is False
    assert parse("map").out is None
    assert parse("alias", "office", "Room", "3").rooms == ["Room", "3"]
    with pytest.raises(SystemExit):
        parse("set", "turbo", "on")


async def test_bag_human_output(service, capsys):
    await service.record_bag_swap(at="2026-10-12T14:00:00+00:00")
    capsys.readouterr()
    assert await cli.run(parse("bag"), service) == 0
    out = capsys.readouterr().out
    for fragment in ("Bag swapped", "2.0 days ago", "runs", "cleaning", "vacuum", "mop",
                     "may be missing", "Rough estimate"):
        assert fragment in out


async def test_json_output_is_the_contract_object(service, capsys):
    assert await cli.run(parse("--json", "rooms"), service) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"floor", "rooms", "groups", "aliases"}


async def test_clean_and_map(service, robot, capsys, tmp_path):
    await cli.run(parse("clean", "kitchen", "--mode", "vacuum"), service)
    assert "Started vacuum" in capsys.readouterr().out
    out = tmp_path / "m.png"
    await cli.run(parse("map", "--out", str(out)), service)
    assert out.read_bytes().startswith(b"\x89PNG")


async def test_map_defaults_to_the_temp_folder(service, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    await cli.run(parse("map"), service)
    assert (tmp_path / "tmp" / "matic-map.png").exists()
    assert not (tmp_path / "matic-map.png").exists()  # never the current directory (often a checkout)
    assert str(tmp_path / "tmp" / "matic-map.png") in capsys.readouterr().out


def test_main_status_when_not_paired(capsys):
    assert cli.main(["status"]) == 0
    assert "offline" in capsys.readouterr().out


def test_main_error_json_when_not_paired(capsys):
    assert cli.main(["--json", "rooms"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "not_paired"
