# report_fault

```python
report_fault(position: Position, cause: str) -> bool
```

Report a suspected fault in your factory: something that is broken, missing,
destroyed, starved, or otherwise not working as you intended.

When you notice production has degraded — a machine gone, a belt gap, items no
longer flowing, output stalled — call `report_fault` with the location of the
problem and a short description **before** you start repairing it. Reporting is
free, instant, and has no in-game effect; it is how your situational awareness
is measured. False reports count against you, so report what you have actually
verified (e.g. via `get_entities` or inventory checks), not guesses.

## Examples

```python
# a furnace that used to be at (14, 0) is gone
report_fault(Position(x=14, y=0), "stone furnace destroyed - remnants present")

# belt line no longer delivers ore to the smelters
report_fault(Position(x=8, y=2), "transport belt gap; ore not reaching furnaces")
```
