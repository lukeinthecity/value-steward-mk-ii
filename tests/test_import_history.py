"""Tests for the world-context history import.

The realism case at the bottom runs against **VS1's actual committed
world-context.jsonl**, checked in as a fixture. Per the code-check playbook
section 4, a fixture hand-typed to look plausible is what let this project's
predecessor ship four population bugs its own tests all passed.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

import pytest

from vs2.world.import_history import (
    existing_keys,
    import_history,
    missing_dates_between,
    row_key,
    validate_row,
    write_snapshot,
)

FIXTURE_GZ = Path(__file__).parent / "fixture_vs1_world_context.jsonl.gz"


@pytest.fixture
def vs1_file(tmp_path: Path) -> Path:
    """VS1's real committed rows, stored gzipped so 136 genuine rows cost ~90KB
    in the repo instead of 1.2MB. Decompressed verbatim -- not trimmed, not
    tidied. A fixture cleaner than production is not a test (playbook, 4)."""

    out = tmp_path / "vs1-world-context.jsonl"
    with gzip.open(FIXTURE_GZ, "rb") as handle:
        out.write_bytes(handle.read())
    return out


def row(day: str, slot: str | None = None, generated_at: str | None = None) -> dict:
    return {
        "date": day,
        "generated_at": generated_at or f"{day}T20:00:00.000Z",
        "summary": "a summary",
        "tags": {
            "macro_risk": 0.1,
            "rate_hawkishness": 0.5,
            "geopolitical_tension": 0.2,
            "energy_shock_risk": 0.0,
            "recession_fear": None,
        },
        "sources_used": ["un-news"],
        "raw_count": 12,
        "notes": None,
        "errors": [],
        "slot": slot,
    }


def write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


# --- validation ---------------------------------------------------------------


def test_a_well_formed_row_passes() -> None:
    assert validate_row(row("2026-05-05")) is None


def test_a_missing_required_field_is_named() -> None:
    bad = row("2026-05-05")
    del bad["tags"]

    reason = validate_row(bad)
    assert reason is not None and "tags" in reason


def test_a_tag_outside_zero_to_one_is_refused() -> None:
    """The schema bounds these. A 1.4 means something upstream is wrong, and
    accepting it would carry the fault into every later average."""

    bad = row("2026-05-05")
    bad["tags"]["macro_risk"] = 1.4

    reason = validate_row(bad)
    assert reason is not None and "outside [0,1]" in reason


def test_a_null_tag_is_allowed() -> None:
    """VS1 writes null when it could not score a tag. That is a real state and
    is not the same as zero."""

    ok = row("2026-05-05")
    ok["tags"]["macro_risk"] = None

    assert validate_row(ok) is None


def test_a_boolean_is_not_accepted_as_a_number() -> None:
    """bool is a subclass of int in Python; True would otherwise sail through
    a naive numeric check and read as a tag value of 1.0."""

    bad = row("2026-05-05")
    bad["tags"]["macro_risk"] = True

    assert validate_row(bad) is not None


def test_a_malformed_date_is_refused() -> None:
    bad = row("not-a-date")
    assert validate_row(bad) is not None


# --- identity and idempotency --------------------------------------------------


def test_rows_from_different_slots_on_one_date_are_distinct() -> None:
    """VS1 wrote several rows per date from four intraday slots, so date alone
    is not an identity."""

    assert row_key(row("2026-05-05", slot="midday")) != row_key(
        row("2026-05-05", slot="pre_close")
    )


def test_reimporting_the_same_file_adds_nothing(tmp_path: Path) -> None:
    source = tmp_path / "src.jsonl"
    dest = tmp_path / "dest.jsonl"
    write(source, [row("2026-05-05"), row("2026-05-06")])

    first = import_history(source, dest, since=date(2026, 5, 5))
    second = import_history(source, dest, since=date(2026, 5, 5))

    assert first.imported == 2
    assert second.imported == 0
    assert second.already_present == 2
    assert len(dest.read_text(encoding="utf-8").strip().split("\n")) == 2


def test_a_grown_source_imports_only_the_new_rows(tmp_path: Path) -> None:
    """The handover case: VS1 keeps appending until cutover."""

    source = tmp_path / "src.jsonl"
    dest = tmp_path / "dest.jsonl"
    write(source, [row("2026-05-05")])
    import_history(source, dest, since=date(2026, 5, 5))

    write(source, [row("2026-05-05"), row("2026-05-06")])
    second = import_history(source, dest, since=date(2026, 5, 5))

    assert second.imported == 1
    assert second.already_present == 1


# --- population completeness ---------------------------------------------------


def test_every_source_row_is_accounted_for(tmp_path: Path) -> None:
    """The playbook's most-repeated defect class: a row vanishing silently."""

    source = tmp_path / "src.jsonl"
    dest = tmp_path / "dest.jsonl"
    good = row("2026-05-05")
    bad = row("2026-05-06")
    del bad["summary"]
    write(source, [good, bad])

    report = import_history(source, dest, since=date(2026, 5, 5))

    assert report.source_rows == 2
    assert report.imported == 1
    assert len(report.problems) == 1
    assert report.accounted_for is True


def test_an_unparseable_line_is_reported_not_skipped(tmp_path: Path) -> None:
    source = tmp_path / "src.jsonl"
    dest = tmp_path / "dest.jsonl"
    source.write_text(
        json.dumps(row("2026-05-05")) + "\n" + "{not json\n", encoding="utf-8"
    )

    report = import_history(source, dest, since=date(2026, 5, 5))

    assert report.source_rows == 2
    assert report.imported == 1
    assert any("unparseable" in p.reason for p in report.problems)
    assert report.accounted_for is True


def test_rows_before_the_working_series_are_excluded_and_counted(
    tmp_path: Path,
) -> None:
    """The pre-gap block must not be stitched onto the working series, and
    must not vanish from the accounting either."""

    source = tmp_path / "src.jsonl"
    dest = tmp_path / "dest.jsonl"
    write(source, [row("2026-03-01"), row("2026-05-05")])

    report = import_history(source, dest, since=date(2026, 5, 5))

    assert report.imported == 1
    assert any("before the working series" in p.reason for p in report.problems)
    assert report.accounted_for is True


# --- gaps ----------------------------------------------------------------------


def test_a_missing_day_inside_the_range_is_reported() -> None:
    missing = missing_dates_between(
        [date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 8)]
    )

    assert missing == [date(2026, 5, 7)]


def test_weekend_and_weekday_gaps_are_distinguished() -> None:
    """A weekend gap in an RSS pipeline may be ordinary; a weekday gap is a
    collection failure. Both are reported, neither is assumed."""

    # 2026-05-08 is a Friday; 09 Sat, 10 Sun, 11 Mon.
    source_dates = [date(2026, 5, 8), date(2026, 5, 12)]
    missing = missing_dates_between(source_dates)

    assert missing == [date(2026, 5, 9), date(2026, 5, 10), date(2026, 5, 11)]
    assert [d for d in missing if d.weekday() < 5] == [date(2026, 5, 11)]


def test_a_contiguous_range_reports_no_gaps() -> None:
    assert missing_dates_between([date(2026, 5, 5), date(2026, 5, 6)]) == []


def test_a_single_date_cannot_have_a_gap() -> None:
    assert missing_dates_between([date(2026, 5, 5)]) == []


# --- snapshots -----------------------------------------------------------------


def test_a_snapshot_round_trips_byte_identically(tmp_path: Path) -> None:
    """The snapshot is the copy that survives a mistake on the working file.
    A lossy one would be worse than none, because it would look like a backup."""

    source = tmp_path / "live.jsonl"
    write(source, [row("2026-05-05"), row("2026-05-06")])
    snapshot = tmp_path / "history" / "snap.jsonl.gz"

    size = write_snapshot(source, snapshot)

    assert size > 0
    with gzip.open(snapshot, "rb") as handle:
        assert handle.read() == source.read_bytes()


def test_existing_keys_is_empty_for_a_missing_destination(tmp_path: Path) -> None:
    assert existing_keys(tmp_path / "absent.jsonl") == set()


# --- against VS1's real committed data -----------------------------------------


def test_the_real_vs1_file_validates_and_imports(tmp_path: Path, vs1_file: Path) -> None:
    """VS1's actual committed rows, not a plausible-looking imitation."""

    dest = tmp_path / "dest.jsonl"
    report = import_history(vs1_file, dest)

    assert report.source_rows == 136
    assert report.imported == 136
    assert report.problems == []
    assert report.accounted_for is True
    assert report.first_date == date(2026, 1, 23)
    assert report.last_date == date(2026, 3, 20)


def test_the_real_file_has_the_gappy_shape_the_import_must_surface(
    tmp_path: Path, vs1_file: Path
) -> None:
    """56 calendar days holding 30 days of data. The gaps are real, and a
    report that hid them would misrepresent the dataset."""

    report = import_history(vs1_file, tmp_path / "dest.jsonl")

    assert len(report.missing_dates) > 0
    assert len(report.missing_weekdays) < len(report.missing_dates)


def test_the_real_file_is_idempotent_on_reimport(tmp_path: Path, vs1_file: Path) -> None:
    dest = tmp_path / "dest.jsonl"
    import_history(vs1_file, dest)
    again = import_history(vs1_file, dest)

    assert again.imported == 0
    assert again.already_present == 136


def test_two_rows_sharing_date_slot_and_generated_at_are_both_kept(
    tmp_path: Path, vs1_file: Path
) -> None:
    """A real case from VS1's data, not a hypothetical: 2026-03-12 / pre_close
    / 2026-03-13T02:54:57.838Z appears twice, the rows differing only in
    `scout_cached`. Keying identity on that triple would silently discard one
    of them. Only byte-identical rows may collapse."""

    dest = tmp_path / "dest.jsonl"
    report = import_history(vs1_file, dest)

    assert report.imported == 136
    assert report.already_present == 0

    written = [json.loads(line) for line in dest.read_text().strip().split("\n")]
    clashing = [
        r
        for r in written
        if r.get("date") == "2026-03-12"
        and r.get("slot") == "pre_close"
        and r.get("generated_at") == "2026-03-13T02:54:57.838Z"
    ]
    assert len(clashing) == 2
    assert {r.get("scout_cached") for r in clashing} == {None, True}


def test_a_byte_identical_duplicate_still_collapses(tmp_path: Path) -> None:
    source = tmp_path / "src.jsonl"
    dest = tmp_path / "dest.jsonl"
    write(source, [row("2026-05-05"), row("2026-05-05")])

    report = import_history(source, dest, since=date(2026, 5, 5))

    assert report.imported == 1
    assert report.already_present == 1
