"""The world layer must not reach the trading path, and vice versa.

DESIGN.md defers world-state *gating* until the crossover baseline has produced
a readable result: "Adding it before the baseline reads would repeat VS1's
central mistake: introducing a second mechanism before the first one was
measured." Collecting the data is not that mechanism -- but the only thing
keeping collection from becoming a gate is that nothing wires the two together.

A convention nobody can check is not a boundary. This asserts it against the
actual import graph, so a future change that crosses the line fails here rather
than depending on someone noticing in review.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "vs2"

# The one permitted crossing: the world layer may read the market timezone
# rather than inventing a second notion of what "Monday" means.
ALLOWED_FROM_WORLD = {"vs2.data.market_calendar"}

TRADING_MODULES = ("vs2.core", "vs2.data", "vs2.run_daily", "vs2.analysis")


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def python_files(package: str) -> list[Path]:
    root = SRC / package if package else SRC
    if not root.exists():
        return []
    return sorted(root.rglob("*.py"))


def test_the_world_layer_does_not_import_the_trading_path() -> None:
    offenders: list[str] = []
    for path in python_files("world"):
        for name in imports_of(path):
            if name in ALLOWED_FROM_WORLD:
                continue
            if any(name == m or name.startswith(m + ".") for m in TRADING_MODULES):
                offenders.append(f"{path.name} imports {name}")

    assert not offenders, (
        "the world layer reached into the trading path: "
        + "; ".join(offenders)
        + ". The gate is deferred until the crossover baseline reads."
    )


def test_the_trading_path_does_not_import_the_world_layer() -> None:
    """The direction that would actually turn collection into a gate."""

    offenders: list[str] = []
    for package in ("core", "data", "analysis"):
        for path in python_files(package):
            for name in imports_of(path):
                if name == "vs2.world" or name.startswith("vs2.world."):
                    offenders.append(f"{path.name} imports {name}")

    for path in (SRC / "run_daily.py", SRC / "report.py"):
        if path.exists():
            for name in imports_of(path):
                if name == "vs2.world" or name.startswith("vs2.world."):
                    offenders.append(f"{path.name} imports {name}")

    assert not offenders, (
        "the trading path reached into the world layer: "
        + "; ".join(offenders)
        + ". World state must not influence a decision while the gate is deferred."
    )


def test_run_daily_does_not_invoke_the_world_entrypoint() -> None:
    """Separate entrypoints, so a world-fetch failure cannot abort a cycle
    that is about to place orders."""

    source = (SRC / "run_daily.py").read_text(encoding="utf-8")

    assert "run_world" not in source
    assert "vs2.world" not in source
