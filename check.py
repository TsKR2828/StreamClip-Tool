"""快速診斷：檢查 segments.json 的涵蓋率和空白。"""
import json
import sys
from pathlib import Path

# 自動找最新的 output 資料夾
out_dirs = sorted(Path("output").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
if not out_dirs:
    sys.exit("output/ 裡沒有資料夾")

seg_file = out_dirs[0] / "segments.json"
if not seg_file.exists():
    sys.exit(f"找不到 {seg_file}")

segs = json.loads(seg_file.read_text(encoding="utf-8"))
print(f"資料夾: {out_dirs[0].name}")
print(f"總段數: {len(segs)}")
print(f"第一段: {segs[0]['start']:.1f}s ({segs[0]['start']/60:.1f} 分)")
print(f"最後段: {segs[-1]['end']:.1f}s ({segs[-1]['end']/60:.1f} 分)")

# 找最大空白
gaps = []
for i in range(len(segs) - 1):
    gap = segs[i + 1]["start"] - segs[i]["end"]
    gaps.append((gap, segs[i]["end"]))

gaps.sort(reverse=True)
print(f"\n最長的 5 個空白:")
for g, t in gaps[:5]:
    m = int(t // 60)
    s = t % 60
    print(f"  {g:.1f}s 空白, 在 {m:02d}:{s:05.2f}")

# 涵蓋率
total_speech = sum(s["end"] - s["start"] for s in segs)
total_audio = segs[-1]["end"] - segs[0]["start"]
print(f"\n語音涵蓋率: {total_speech:.0f}s / {total_audio:.0f}s = {total_speech/total_audio*100:.1f}%")
