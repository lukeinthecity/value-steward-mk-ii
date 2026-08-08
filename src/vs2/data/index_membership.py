"""Fetch index constituent symbols from Wikipedia's community-maintained lists.

Both the Dow Jones Industrial Average and the S&P 500 pages expose their
membership in a table with `id="constituents"` and a `Symbol` column, so one
parser serves both.

**The Dow 30 is the configured universe** -- see DESIGN.md for the measurement
that chose it over the S&P 500. The S&P URL is kept because the wider list is
useful for analysis, and because a future decision to widen the universe should
not require rewriting this module.

Verified 2026-08-07: all 503 S&P symbols and all 30 Dow symbols parse to
tradable, active Alpaca assets with no reformatting, including the dotted
tickers (BRK.B, BF.B) -- see docs/API_MENU.md for the cross-check.
"""

from __future__ import annotations

import logging
import os
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DOW_URL = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# The configured universe. Changing this is a decision-affecting change.
UNIVERSE_URL = DOW_URL
UNIVERSE_MIN_EXPECTED = 25

CONSTITUENTS_TABLE_ID = "constituents"
USER_AGENT = "value-steward-mk-ii research (contact: luke.shefski@gmail.com)"


class _ConstituentsTableParser(HTMLParser):
    """Extracts rows from the table with id="constituents" only."""

    def __init__(self) -> None:
        super().__init__()
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._cell_text = ""
        self._row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        if tag == "table" and attr_dict.get("id") == CONSTITUENTS_TABLE_ID:
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._row = []
        elif self._in_table and tag in ("td", "th"):
            self._in_cell = True
            self._cell_text = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._in_table = False
        elif tag == "tr" and self._in_row:
            self._in_row = False
            if self._row:
                self.rows.append(self._row)
        elif tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._row.append(self._cell_text.strip())

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text += data


def fetch_constituents_html(url: str = UNIVERSE_URL, timeout: float = 20.0) -> str:
    """Fetch the raw HTML of the constituents page. The only network call here."""

    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed https URL
        return response.read().decode("utf-8")


def parse_constituents(html: str, *, min_expected: int = UNIVERSE_MIN_EXPECTED) -> list[str]:
    """Return sorted, de-duplicated ticker symbols from the constituents table.

    Pure function -- no network, no filesystem -- so it can be tested against a
    saved fixture instead of live Wikipedia. `min_expected` guards against a
    silently truncated or restructured page; tests pass a smaller value since
    the fixture is deliberately a handful of real rows, not the full table.
    Pass a larger value when parsing the S&P page, where a 25-row result would
    itself indicate a parse failure.
    """

    parser = _ConstituentsTableParser()
    parser.feed(html)
    if not parser.rows:
        raise ValueError(
            f"no rows found in table id={CONSTITUENTS_TABLE_ID!r}; "
            "the page structure may have changed"
        )

    header, *data_rows = parser.rows
    try:
        symbol_index = header.index("Symbol")
    except ValueError as exc:
        raise ValueError(f"expected a 'Symbol' column, got header {header!r}") from exc

    symbols = {row[symbol_index] for row in data_rows if row[symbol_index]}
    if len(symbols) < min_expected:
        raise ValueError(
            f"parsed only {len(symbols)} symbols, expected at least "
            f"{min_expected} -- treating this as a parse failure"
        )
    return sorted(symbols)


def write_universe(symbols: list[str], path: Path) -> None:
    """Write one symbol per line, atomically (tmp file + os.replace)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".universe-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(symbols) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise


def refresh_universe(
    path: Path, url: str = UNIVERSE_URL, *, min_expected: int = UNIVERSE_MIN_EXPECTED
) -> list[str]:
    """Fetch, parse, and write the universe file. The composed entrypoint."""

    html = fetch_constituents_html(url)
    symbols = parse_constituents(html, min_expected=min_expected)
    write_universe(symbols, path)
    logger.info("wrote %d symbols to %s", len(symbols), path)
    return symbols


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo_root = Path(__file__).resolve().parents[3]
    universe_path = repo_root / "config" / "universe.txt"
    refresh_universe(universe_path)


if __name__ == "__main__":
    main()
