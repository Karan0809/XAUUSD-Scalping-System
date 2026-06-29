#!/usr/bin/env python3
import json
from collections import Counter
d = json.load(open("mindspace_results.json"))
ts = d.get("trades", [])
if not ts:
    print("No trades in output")
    exit()
wins = [t for t in ts if t["profit"] > 0]
losses = [t for t in ts if t["profit"] <= 0]
print(f"Total: {len(ts)}, WR: {len(wins)/len(ts)*100:.1f}%")
print(f"Avg win: {sum(t['profit'] for t in wins)/len(wins):.2f}")
print(f"Avg loss: {sum(t['profit'] for t in losses)/len(losses):.2f}")
print(f"Max win: {max(t['profit'] for t in wins):.2f}")
print(f"Max loss: {min(t['profit'] for t in losses):.2f}")
print(f"Largest win: reason={max(wins, key=lambda t:t['profit'])['exit_reason']} bars={max(wins, key=lambda t:t['profit'])['bars_held']}")
print()
print("First 15 trades:")
for t in ts[:15]:
    print(f"  {t['exit_reason']:6s} profit={t['profit']:>8.2f} level={t['level_type']:6s} tf={t['tf']:3s} entry={t['entry']:.2f} exit={t['exit']:.2f} lot={t['lot_size']:.2f} bars={t['bars_held']}")
print()
reasons = Counter(t["exit_reason"] for t in ts)
for r, c in reasons.most_common():
    print(f"  {r}: {c}")
