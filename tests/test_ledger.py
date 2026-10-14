import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from matic_desk.ledger import Ledger, iso

NOW = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)
DOCKED = {"connected": True, "activity": "docked", "bag_full": False, "bag_missing": False}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def run(key: str, started: str, *, duration: int = 1000, vacuum: int = 600, mop: int = 300) -> dict:
    return {
        "key": key,
        "started_at": started + "+00:00",
        "ended_at": None,
        "duration_seconds": duration,
        "vacuum_seconds": vacuum,
        "mop_seconds": mop,
        "rooms": ["Kitchen 1"],
        "modes": ["mop", "vacuum"],
        "completed": True,
    }


@pytest.fixture
def ledger() -> Ledger:
    return Ledger(":memory:")


def test_ingest_upserts_idempotently_and_tracks_growing_runs(ledger):
    first = run("aa", "2026-10-10T10:00:00", duration=100, vacuum=50)
    assert ledger.ingest([first], NOW) is True
    assert ledger.ingest([first], NOW) is False
    assert ledger.ingest([{**first, "duration_seconds": 200, "vacuum_seconds": 120}], NOW) is True
    [stored] = ledger.sessions()
    assert stored["duration_seconds"] == 200 and stored["vacuum_seconds"] == 120
    assert stored["started_at"] == "2026-10-10T10:00:00Z"


def test_first_ingest_is_recorded_once(ledger):
    ledger.ingest([], at("2026-10-01T00:00:00"))
    ledger.ingest([], at("2026-10-02T00:00:00"))
    assert ledger.meta("first_ingest_at") == "2026-10-01T00:00:00Z"


def test_ledger_file_uses_wal(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    mode = ledger._db.execute("PRAGMA journal_mode").fetchone()[0]
    ledger.close()
    assert mode == "wal"


def test_report_without_swap(ledger):
    ledger.ingest([run("aa", "2026-10-10T10:00:00")], NOW)
    report = ledger.bag_report(NOW, DOCKED)
    for field in ("swapped_at", "days_since", "runs_since", "cleaning_seconds_since",
                  "vacuum_seconds_since", "mop_seconds_since", "counted_from"):
        assert report[field] is None
    assert report["estimate"]["basis"] == "unknown"
    assert "record" in report["estimate"]["text"].lower()
    assert report["previous_bags"] == []


def test_complete_when_ledger_predates_swap(ledger):
    ledger.ingest([], at("2026-10-01T00:00:00"))
    ledger.record_swap(at("2026-10-06T12:00:00"), None, NOW)
    ledger.ingest(
        [
            run("old", "2026-10-05T09:00:00"),
            run("a", "2026-10-07T09:00:00", duration=1000, vacuum=600, mop=300),
            run("b", "2026-10-08T09:00:00", duration=2000, vacuum=900, mop=800),
        ],
        NOW,
    )
    report = ledger.bag_report(NOW, DOCKED)
    assert report["swapped_at"] == "2026-10-06T12:00:00Z"
    assert report["complete"] is True
    assert report["counted_from"] == "2026-10-06T12:00:00Z"
    assert report["runs_since"] == 2
    assert report["cleaning_seconds_since"] == 3000
    assert report["vacuum_seconds_since"] == 1500
    assert report["mop_seconds_since"] == 1100
    assert report["days_since"] == 8.1


def test_complete_when_ledger_holds_an_older_run(ledger):
    ledger.record_swap(at("2026-10-06T12:00:00"), None, NOW)
    ledger.ingest([run("old", "2026-10-05T09:00:00"), run("a", "2026-10-07T09:00:00")], NOW)
    report = ledger.bag_report(NOW, DOCKED)
    assert report["complete"] is True
    assert report["runs_since"] == 1


def test_incomplete_counts_from_earliest_run_after_swap(ledger):
    ledger.record_swap(at("2026-10-06T12:00:00"), None, NOW)
    ledger.ingest([run("a", "2026-10-09T09:00:00"), run("b", "2026-10-08T09:00:00")], NOW)
    report = ledger.bag_report(NOW, DOCKED)
    assert report["complete"] is False
    assert report["counted_from"] == "2026-10-08T09:00:00Z"


def test_incomplete_without_runs_counts_from_swap(ledger):
    ledger.record_swap(at("2026-10-06T12:00:00"), None, NOW)
    ledger.ingest([], NOW)
    report = ledger.bag_report(NOW, DOCKED)
    assert report["complete"] is False
    assert report["counted_from"] == "2026-10-06T12:00:00Z"
    assert report["runs_since"] == 0


def test_previous_bags_and_learned_estimate_uses_median(ledger):
    ledger.ingest([], at("2026-08-31T00:00:00"))
    for swap in ("2026-09-01T00:00:00", "2026-09-10T00:00:00", "2026-09-20T00:00:00", "2026-10-01T00:00:00"):
        ledger.record_swap(at(swap), None, NOW)
    ledger.ingest(
        [
            run("b1a", "2026-09-02T09:00:00", vacuum=3600),
            run("b1b", "2026-09-04T09:00:00", vacuum=3600),
            run("b1c", "2026-09-08T09:00:00", vacuum=3600),  # after the bag-full event
            run("b2a", "2026-09-11T09:00:00", vacuum=3600),
            run("b2b", "2026-09-12T09:00:00", vacuum=7200),
            run("b3a", "2026-09-21T09:00:00", vacuum=3600),
            run("now", "2026-10-02T09:00:00", vacuum=1800),
        ],
        NOW,
    )
    ledger.record_bag_flags(False, False, at("2026-09-01T01:00:00"))
    ledger.record_bag_flags(True, False, at("2026-09-06T00:00:00"))
    ledger.record_bag_flags(False, False, at("2026-09-10T00:00:00"))
    ledger.record_bag_flags(True, False, at("2026-09-15T00:00:00"))
    ledger.record_bag_flags(False, False, at("2026-09-20T00:00:00"))

    report = ledger.bag_report(NOW, DOCKED)
    bags = report["previous_bags"]
    assert [bag["swapped_at"] for bag in bags] == [
        "2026-09-20T00:00:00Z", "2026-09-10T00:00:00Z", "2026-09-01T00:00:00Z"
    ]
    assert bags[0]["replaced_at"] == "2026-10-01T00:00:00Z"
    assert bags[0]["vacuum_seconds_to_full"] is None
    assert bags[1]["vacuum_seconds_to_full"] == 10800
    assert bags[2]["vacuum_seconds_to_full"] == 7200
    assert bags[2]["vacuum_seconds"] == 10800 and bags[2]["cleaning_seconds"] == 3000

    estimate = report["estimate"]
    assert estimate["basis"] == "learned"
    assert estimate["vacuum_hours_left"] == 2.0  # median 9000 s - 1800 s used
    assert "estimate" in estimate["text"].lower()
    assert report["last_bag_full_at"] is None  # no full event since the current swap


def test_guidance_estimate(ledger):
    ledger.record_swap(at("2026-10-12T14:00:00"), None, NOW)
    estimate = ledger.bag_report(NOW, DOCKED)["estimate"]
    assert estimate == {
        "basis": "guidance",
        "days_left": 5.0,
        "vacuum_hours_left": None,
        "text": estimate["text"],
    }
    assert "estimate" in estimate["text"].lower() and "7 days" in estimate["text"]


def test_earlier_swap_corrects_a_mistaken_one(ledger):
    assert ledger.record_swap(NOW, None, NOW) == 0  # "I swapped the bag" pressed by mistake
    assert ledger.record_swap(at("2026-10-06T12:00:00"), "fix", NOW) == 1  # the real date
    report = ledger.bag_report(NOW, DOCKED)
    assert report["swapped_at"] == "2026-10-06T12:00:00Z"
    assert report["previous_bags"] == []  # no made-up bag between the two dates
    assert report["estimate"]["days_left"] == 0.0


def test_later_swap_starts_a_new_bag_and_same_time_is_not_doubled(ledger):
    ledger.record_swap(at("2026-10-06T12:00:00"), None, NOW)
    assert ledger.record_swap(at("2026-10-12T14:00:00"), None, NOW) == 0
    assert ledger.record_swap(at("2026-10-12T14:00:00"), None, NOW) == 1
    report = ledger.bag_report(NOW, DOCKED)
    assert report["swapped_at"] == "2026-10-12T14:00:00Z"
    assert [(b["swapped_at"], b["replaced_at"]) for b in report["previous_bags"]] == [
        ("2026-10-06T12:00:00Z", "2026-10-12T14:00:00Z")
    ]


def test_import_swap_once(ledger):
    assert ledger.import_swap_once(at("2026-10-06T12:00:00"), NOW) is True
    assert ledger.bag_report(NOW, DOCKED)["swapped_at"] == "2026-10-06T12:00:00Z"
    ledger.record_swap(at("2026-10-01T12:00:00"), None, NOW)  # user corrects it
    assert ledger.import_swap_once(at("2026-10-06T12:00:00"), NOW) is False  # never re-imported
    assert ledger.bag_report(NOW, DOCKED)["swapped_at"] == "2026-10-01T12:00:00Z"


def test_import_swap_skipped_when_a_swap_exists():
    ledger = Ledger(":memory:")
    ledger.record_swap(at("2026-10-12T14:00:00"), None, NOW)
    assert ledger.import_swap_once(at("2026-10-06T12:00:00"), NOW) is False
    assert ledger.swaps() == [at("2026-10-12T14:00:00")]


def test_previous_bag_not_covered_by_the_ledger_has_no_capacity(ledger):
    # The ledger starts after bag 1 was put in; the robot had already forgotten its early runs.
    ledger.record_swap(at("2026-09-01T00:00:00"), None, NOW)
    ledger.ingest([run("late", "2026-09-08T09:00:00", vacuum=1800)], at("2026-09-08T12:00:00"))
    ledger.record_bag_flags(True, False, at("2026-09-09T00:00:00"))
    ledger.record_swap(at("2026-09-10T00:00:00"), None, NOW)
    report = ledger.bag_report(NOW, DOCKED)
    assert report["previous_bags"][0]["vacuum_seconds_to_full"] is None
    assert report["estimate"]["basis"] == "guidance"


def test_concurrent_creation_of_a_new_ledger(tmp_path, monkeypatch):
    """Several processes opening a brand-new ledger at once used to hit "database is locked"."""
    connect = sqlite3.connect
    errors: list[str] = []
    for round_ in range(15):
        path = tmp_path / f"ledger{round_}.sqlite"
        barrier = threading.Barrier(4)

        def synced_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            barrier.wait()  # line every opener up right before the WAL switch
            return db

        monkeypatch.setattr(sqlite3, "connect", synced_connect)

        def open_ledger() -> None:
            try:
                Ledger(path).close()
            except sqlite3.Error as err:
                errors.append(str(err))

        threads = [threading.Thread(target=open_ledger) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert errors == []


def test_guidance_past_seven_days(ledger):
    ledger.record_swap(at("2026-10-06T12:00:00"), None, NOW)
    estimate = ledger.bag_report(NOW, DOCKED)["estimate"]
    assert estimate["days_left"] == 0.0
    assert "due" in estimate["text"]


def test_bag_flag_transitions(ledger):
    t = at("2026-10-10T00:00:00")
    assert ledger.record_bag_flags(None, None, t) == []
    assert ledger.record_bag_flags(False, False, t) == []  # first observation, nothing changed
    assert ledger.record_bag_flags(False, None, t) == []
    assert ledger.record_bag_flags(True, None, at("2026-10-11T00:00:00")) == ["bag_full_on"]
    assert ledger.record_bag_flags(True, True, at("2026-10-12T00:00:00")) == ["bag_missing_on"]
    assert ledger.record_bag_flags(None, None, at("2026-10-12T01:00:00")) == []
    assert ledger.record_bag_flags(False, False, at("2026-10-13T00:00:00")) == [
        "bag_full_off", "bag_missing_off"
    ]


def test_first_observation_full_is_an_event(ledger):
    assert ledger.record_bag_flags(True, False, NOW) == ["bag_full_on"]


def test_last_bag_full_at_only_counts_since_swap(ledger):
    ledger.record_bag_flags(True, False, at("2026-10-02T00:00:00"))
    ledger.record_bag_flags(False, False, at("2026-10-03T00:00:00"))
    ledger.record_swap(at("2026-10-06T12:00:00"), None, NOW)
    assert ledger.bag_report(NOW, DOCKED)["last_bag_full_at"] is None
    ledger.record_bag_flags(True, False, at("2026-10-12T00:00:00"))
    ledger.record_bag_flags(False, False, at("2026-10-12T06:00:00"))
    ledger.record_bag_flags(True, False, at("2026-10-13T00:00:00"))
    assert ledger.bag_report(NOW, DOCKED)["last_bag_full_at"] == "2026-10-13T00:00:00Z"


def test_in_progress_and_full_bag_text(ledger):
    ledger.record_swap(at("2026-10-12T14:00:00"), None, NOW)
    status = {"connected": True, "activity": "cleaning", "bag_full": True, "bag_missing": False}
    report = ledger.bag_report(NOW, status)
    assert report["in_progress"] is True
    assert report["bag_full"] is True and report["bag_missing"] is False
    assert report["estimate"]["text"].startswith("The robot reports the bag is full")


def test_full_bag_text_without_swap(ledger):
    status = {"connected": True, "activity": "docked", "bag_full": True, "bag_missing": False}
    assert "asking for a swap now" in ledger.bag_report(NOW, status)["estimate"]["text"]


def test_offline_status_leaves_flags_unknown(ledger):
    ledger.record_swap(at("2026-10-12T14:00:00"), None, NOW)
    report = ledger.bag_report(NOW, {"connected": False, "activity": "offline", "bag_full": None})
    assert report["bag_full"] is None and report["bag_missing"] is None
    assert report["in_progress"] is False


def test_iso_format():
    assert iso(datetime(2026, 10, 14, 13, 53, 10, 999, tzinfo=UTC)) == "2026-10-14T13:53:10Z"
