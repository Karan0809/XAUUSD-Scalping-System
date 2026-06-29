import json
d = json.load(open("mindspace_results.json"))
ts = d.get("trades", [])
print(f"Total records: {len(ts)}")
big = [t for t in ts if abs(t["profit"]) > 150]
print(f"Trades with loss/size > 150: {len(big)}")
for t in big:
    print(f"  profit={t['profit']:>8.2f} entry={t['entry']:.2f} exit={t['exit']:.2f} lot={t['lot_size']:.2f} bars={t['bars_held']:2d} reason={t['exit_reason']:6s} level={t['level_type']:6s} tf={t['tf']:3s}")
print()
print("All level types:")
for t in ts:
    print(f"  {t['level_type']:6s} profit={t['profit']:>8.2f} lot={t['lot_size']:.2f}")
