# get_alerts

```python
get_alerts(seconds: int = 10) -> list[str]
```

Get current warnings about problems in your factory from the last `seconds`
game-seconds. Each warning names the entity, its position, and what is wrong:

- `"stone-furnace at (14, 0): out of fuel"`
- `"burner-inserter at (6, 2): inserter waiting for source items"`
- `"burner-mining-drill at (15, 70): nothing to mine"`

Warnings reflect the *current* state: a repaired problem disappears from the
list immediately. An empty list means no known problems — but the alert system
only sees machines with detectable issues; a destroyed machine emits no alert,
so also watch your production numbers and use `get_entities` to verify.

## Examples

```python
warnings = get_alerts(30)
for w in warnings:
    print(w)
if not warnings:
    print("no active alerts")
```
