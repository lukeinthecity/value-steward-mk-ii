"""World-state collection and its history.

Deliberately isolated from the trading path. DESIGN.md defers world-state
*gating* until the crossover baseline has produced a readable result;
collecting the data is not that mechanism, and nothing in this package may
reach a trading decision while the gate is deferred. See the import-boundary
test in tests/test_world_isolation.py, which fails if that line is crossed.
"""
