"""Tests for the S&P 500 constituent parser and universe writer.

fixture_sp500_table.html is a real trimmed slice of Wikipedia's constituents
table (header + 4 real data rows, including both dotted tickers BRK.B and
BF.B), not hand-written idealized markup -- see sp500.py's module docstring
for why that distinction matters here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vs2.data.sp500 import parse_constituents, write_universe

FIXTURE_PATH = Path(__file__).parent / "fixture_sp500_table.html"


def _load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_parses_real_fixture_rows() -> None:
    symbols = parse_constituents(_load_fixture(), min_expected=4)
    assert symbols == ["AOS", "BF.B", "BRK.B", "MMM"]


def test_dotted_tickers_survive_unmodified() -> None:
    symbols = parse_constituents(_load_fixture(), min_expected=4)
    assert "BRK.B" in symbols
    assert "BF.B" in symbols
    assert "BRK-B" not in symbols
    assert "BRKB" not in symbols


def test_missing_symbol_column_raises() -> None:
    html = """
    <table id="constituents">
    <tr><th>Security</th><th>GICS Sector</th></tr>
    <tr><td>3M</td><td>Industrials</td></tr>
    </table>
    """
    with pytest.raises(ValueError, match="Symbol"):
        parse_constituents(html)


def test_missing_table_raises() -> None:
    html = "<html><body><p>no table here</p></body></html>"
    with pytest.raises(ValueError, match="no rows found"):
        parse_constituents(html)


def test_below_min_expected_raises() -> None:
    with pytest.raises(ValueError, match="parsed only"):
        parse_constituents(_load_fixture(), min_expected=500)


def test_write_universe_writes_one_symbol_per_line(tmp_path: Path) -> None:
    # write_universe writes symbols in the order given -- sorting is
    # parse_constituents's job, not the writer's.
    path = tmp_path / "config" / "universe.txt"
    write_universe(["MMM", "AOS", "BRK.B"], path)
    assert path.read_text(encoding="utf-8") == "MMM\nAOS\nBRK.B\n"


def test_write_universe_creates_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config" / "universe.txt"
    write_universe(["AAPL"], path)
    assert path.exists()


def test_write_universe_overwrites_atomically(tmp_path: Path) -> None:
    path = tmp_path / "universe.txt"
    write_universe(["AAPL", "MSFT"], path)
    write_universe(["ONLY"], path)
    assert path.read_text(encoding="utf-8") == "ONLY\n"
    remaining_tmp_files = list(tmp_path.glob(".universe-*"))
    assert remaining_tmp_files == []
