"""
PoC: 吃一個影片/音訊檔,跑 faster-whisper + 音量峰值偵測,
吐帶時間戳的逐字稿 + 精華候選清單。

用途:驗證本機環境能順利跑起來。第一次跑會自動下載模型(large-v3 約 3GB)。

用法:
    python poc.py <input.mp4>
    python poc.py <input.mp4> --model medium
    python poc.py <input.mp4> --peak-db 8     # 提高門檻、只抓更誇張的峰值
    python poc.py <input.mp4> --device cpu

輸出:
    output/<hash>/transcript.md         逐字稿
    output/<hash>/transcript.srt        字幕檔（SRT 格式，可丟剪輯軟體）
    output/<hash>/highlights.md         音量峰值 + 對應台詞
    output/<hash>/silence_bursts.json   長靜音後爆發候選清單
    output/<hash>/segments.json         whisper 快取(下次不用重跑)
    output/<hash>/audio.wav             抽完的音訊(跑完可刪)
    output/<hash>/source.txt            原始檔名記錄
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf
from opencc import OpenCC

_cc = OpenCC("s2twp")  # 簡體 → 台灣正體(含詞彙轉換,例如「软件」→「軟體」)


def to_traditional(text: str) -> str:
    return _cc.convert(text)


def make_output_dir(input_path: Path) -> Path:
    """用檔名的 hash 前 8 碼 + ASCII 安全摘要當資料夾名,避免中文/特殊字元炸路徑。"""
    stem = input_path.stem
    h = hashlib.md5(stem.encode("utf-8")).hexdigest()[:8]
    # 取前 20 個字元當人類可讀提示,過濾掉 Windows 不允許的字元
    safe = "".join(c for c in stem[:20] if c not in r'\/:*?"<>|')
    out_dir = Path("output") / f"{h}_{safe}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 記錄原始檔名方便查對
    source_file = out_dir / "source.txt"
    if not source_file.exists():
        source_file.write_text(str(input_path.name), encoding="utf-8")
    return out_dir


def extract_audio(video_path: Path, out_wav: Path) -> None:
    """用 ffmpeg 抽 16kHz mono wav。需要系統有 ffmpeg。"""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        str(out_wav),
    ]
    print(f"[1/3] 抽音訊 → {out_wav.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def transcribe(wav_path: Path, model_size: str, language: str, device: str,
               cache_path: Path = None) -> list:
    """跑 faster-whisper,回傳 segments list(dict 格式)。
    如果有 cache_path,在模型還活著時就先存 JSON,避免 GPU 釋放 segfault 導致資料遺失。
    """
    from faster_whisper import WhisperModel

    print(f"[2/3] 載入模型 {model_size} (device={device})...")
    compute_type = "float16" if device == "cuda" else "int8"
    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    print(f"      開始辨識...")
    t0 = time.time()
    segments, info = model.transcribe(
        str(wav_path),
        language=language,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    # 轉成 dict list,方便 JSON 快取 + 後續使用
    result = []
    for seg in segments:
        result.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text,
        })
    elapsed = time.time() - t0
    audio_duration = info.duration
    rtf = elapsed / audio_duration if audio_duration else 0
    print(f"      完成:{len(result)} 段, 音訊 {audio_duration:.0f}s, "
          f"耗時 {elapsed:.0f}s, RTF={rtf:.2f}")

    # ★ 在模型還活著時就存快取 + 寫逐字稿,避免 return 後 GPU 釋放 segfault
    if cache_path:
        save_segments_cache(result, cache_path)

    # 主動釋放模型,包在 try 裡防 segfault
    try:
        del model
    except Exception:
        pass

    return result


def save_segments_cache(segments: list, cache_path: Path) -> None:
    """存 whisper 結果到 JSON,下次不用重跑。"""
    cache_path.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      快取 → {cache_path}")


def load_segments_cache(cache_path: Path) -> list:
    """讀取快取的 whisper 結果。"""
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    print(f"[2/3] 沿用快取 {cache_path.name} ({len(data)} 段,跳過 ASR)")
    return data


def detect_volume_peaks(
    wav_path: Path,
    window_sec: float = 1.0,
    threshold_db_above_baseline: float = 6.0,
    merge_gap_sec: float = 2.0,
) -> list:
    """偵測音量峰值。

    流程:
        1. 切 1 秒窗口,算每窗的 RMS,轉 dB
        2. 取所有窗口 RMS_dB 的中位數當基線(穩,不被尖峰拉走)
        3. 任何窗口 > 基線 + threshold_db 視為峰值
        4. 距離 <= merge_gap_sec 的相鄰峰值合併成同一段

    回傳: list of dict {start, end, peak_db, db_above_baseline}
    """
    print(f"[3/3] 分析音量峰值 (門檻 = 基線 + {threshold_db_above_baseline} dB)...")
    audio, sr = sf.read(str(wav_path))
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    win = int(sr * window_sec)
    n_win = len(audio) // win
    rms = np.array([
        np.sqrt(np.mean(audio[i * win:(i + 1) * win] ** 2))
        for i in range(n_win)
    ])
    rms_db = 20 * np.log10(rms + 1e-10)

    baseline = float(np.median(rms_db))
    p90 = float(np.percentile(rms_db, 90))
    p95 = float(np.percentile(rms_db, 95))
    p99 = float(np.percentile(rms_db, 99))
    max_db = float(rms_db.max())
    print(f"      音量分布: median={baseline:.1f} | p90={p90:.1f} | p95={p95:.1f} | "
          f"p99={p99:.1f} | max={max_db:.1f} dB")
    print(f"      → 你的動態範圍 (max - median) = {max_db - baseline:.1f} dB")

    threshold = baseline + threshold_db_above_baseline
    is_peak = rms_db >= threshold

    peaks = []
    i = 0
    merge_gap_win = int(merge_gap_sec / window_sec)
    while i < n_win:
        if not is_peak[i]:
            i += 1
            continue
        start = i
        end = i
        # 往後吃,容許 merge_gap_win 個非峰值窗口
        j = i + 1
        while j < n_win:
            if is_peak[j]:
                end = j
                j += 1
            elif j - end <= merge_gap_win:
                j += 1
            else:
                break
        peak_db = float(rms_db[start:end + 1].max())
        peaks.append({
            "start": start * window_sec,
            "end": (end + 1) * window_sec,
            "peak_db": peak_db,
            "db_above_baseline": peak_db - baseline,
        })
        i = j

    print(f"      基線 {baseline:.1f} dB,門檻 {threshold:.1f} dB,"
          f"找到 {len(peaks)} 個峰值區段")

    # 保底:不管門檻有沒有命中,至少回傳 Top 20 最大聲的 1 秒窗口
    # (合併相鄰)讓 highlights 永遠不會空
    if len(peaks) < 10:
        print(f"      峰值太少,額外取 Top 20 最大聲時刻當保底")
        top_idx = np.argsort(rms_db)[-20:][::-1]
        top_idx_sorted = sorted(top_idx)
        seen_ranges = {(int(p["start"] / window_sec), int(p["end"] / window_sec))
                       for p in peaks}
        for idx in top_idx_sorted:
            if any(s <= idx <= e for s, e in seen_ranges):
                continue
            peaks.append({
                "start": float(idx * window_sec),
                "end": float((idx + 1) * window_sec),
                "peak_db": float(rms_db[idx]),
                "db_above_baseline": float(rms_db[idx] - baseline),
            })
        peaks.sort(key=lambda p: p["start"])

    return peaks


def fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def write_transcript(segments: list, out_md: Path, source_name: str) -> None:
    lines = [f"# Transcript: {source_name}\n"]
    for seg in segments:
        text = to_traditional(seg["text"].strip())
        lines.append(f"`[{fmt_ts(seg['start'])} → {fmt_ts(seg['end'])}]` {text}\n")
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"      寫出 → {out_md}")


def write_srt(segments: list, out_srt: Path) -> None:
    """輸出 SRT 字幕格式（HH:MM:SS,mmm），可直接匯入剪輯軟體。"""
    def srt_ts(sec: float) -> str:
        # 用整數毫秒運算，避免浮點數導致 ms=1000 的格式錯誤
        total_ms = int(round(sec * 1000))
        ms = total_ms % 1000
        total_ms //= 1000
        s = total_ms % 60
        total_ms //= 60
        m = total_ms % 60
        h = total_ms // 60
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    idx = 1
    for seg in segments:
        text = to_traditional(seg["text"].strip())
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{srt_ts(seg['start'])} --> {srt_ts(seg['end'])}")
        lines.append(text)
        lines.append("")
        idx += 1

    # utf-8（不加 BOM），SRT 解析器大多不接受 BOM
    out_srt.write_text("\n".join(lines), encoding="utf-8")
    print(f"      寫出 → {out_srt}")


def load_channel(path: Path) -> dict:
    """載入 channel.yaml 頻道設定。path 為 None 或不存在時回傳預設值。"""
    defaults = {
        "name": "",
        "language": "zh",
        "keywords": {},
        "weights": {
            "volume_spike": 20,
            "keyword_hit": 40,
            "silence_burst": 25,
            "repeated_word": 10,
            "speech_rate_change": 5,
        },
        "highlight": {
            "top_n": 30,
            "min_score": 10,
            "merge_gap_sec": 5.0,
            "padding_sec": 3.0,
        },
    }
    if path is None or not path.exists():
        return defaults
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    for key, val in defaults.items():
        if key not in data:
            data[key] = val
        elif isinstance(val, dict):
            for sub_key, sub_val in val.items():
                data[key].setdefault(sub_key, sub_val)
    return data


def detect_silence_bursts(
    segments: list,
    gap_sec: float = 3.0,
    min_text_chars: int = 6,
    min_duration_sec: float = 1.5,
    large_gap_override_sec: float = 8.0,
) -> list:
    """偵測長靜音後爆發：上一段結束到下一段開始超過 gap_sec 的時間點。

    過濾條件（以下任一不符則排除，除非 gap >= large_gap_override_sec）：
    - text 長度 >= min_text_chars（排除「好」「如果自己」等短句雜訊）
    - segment 持續時長 >= min_duration_sec（排除太短的片段）

    回傳: list of dict {start, end, gap_before_sec, text, reason}
    """
    if len(segments) < 2:
        return []
    sorted_segs = sorted(segments, key=lambda s: s["start"])
    bursts = []
    for i in range(1, len(sorted_segs)):
        prev = sorted_segs[i - 1]
        curr = sorted_segs[i]
        gap = curr["start"] - prev["end"]
        if gap < gap_sec:
            continue
        text = to_traditional(curr["text"].strip())
        duration = curr["end"] - curr["start"]
        is_large_gap = gap >= large_gap_override_sec
        if not is_large_gap and (len(text) < min_text_chars or duration < min_duration_sec):
            continue
        bursts.append({
            "start": round(curr["start"], 2),
            "end": round(curr["end"], 2),
            "gap_before_sec": round(gap, 2),
            "text": text,
            "reason": "silence_burst",
        })
    return bursts


def merge_signals(
    volume_peaks: list,
    silence_bursts: list,
    keyword_hits: list = None,
    channel: dict = None,
) -> list:
    """合併所有訊號源成統一的候選清單。

    每個訊號先在自身類型內 normalize 成 0-100，再乘以 channel weights。
    候選可能在時間上重疊（merge_highlights 負責合併）。

    回傳: list of dict {start, end, score, reasons}，按 start 排序
    """
    if channel is None:
        channel = load_channel(None)
    weights = channel["weights"]
    candidates = []

    # — 音量峰值 —
    if volume_peaks:
        max_db = max((p["db_above_baseline"] for p in volume_peaks), default=0)
        if max_db > 0:
            w = weights.get("volume_spike", 20)
            for p in volume_peaks:
                raw = p["db_above_baseline"] / max_db * 100
                candidates.append({
                    "start": p["start"],
                    "end": p["end"],
                    "score": round(raw * w / 100, 1),
                    "reasons": [f"volume(+{p['db_above_baseline']:.1f}dB)"],
                })

    # — 長靜音後爆發 —
    if silence_bursts:
        max_gap = max((b["gap_before_sec"] for b in silence_bursts), default=0)
        if max_gap > 0:
            w = weights.get("silence_burst", 25)
            for b in silence_bursts:
                raw = b["gap_before_sec"] / max_gap * 100
                candidates.append({
                    "start": b["start"],
                    "end": b["end"],
                    "score": round(raw * w / 100, 1),
                    "reasons": [f"silence({b['gap_before_sec']:.1f}s)"],
                })

    # — 關鍵字命中（T4 接入口，目前為空）—
    if keyword_hits:
        max_kw = max((kh["score"] for kh in keyword_hits), default=0)
        if max_kw > 0:
            w = weights.get("keyword_hit", 40)
            for kh in keyword_hits:
                raw = kh["score"] / max_kw * 100
                candidates.append({
                    "start": kh["start"],
                    "end": kh["end"],
                    "score": round(raw * w / 100, 1),
                    "reasons": kh.get("reasons", ["keyword"]),
                })

    candidates.sort(key=lambda c: c["start"])
    print(f"      訊號合併: {len(candidates)} 個候選 "
          f"(volume={len(volume_peaks or [])}, "
          f"silence={len(silence_bursts or [])}, "
          f"keyword={len(keyword_hits or [])})")
    return candidates


def write_highlights(peaks: list, segments: list, out_md: Path, source_name: str) -> None:
    """把音量峰值對齊逐字稿輸出。"""
    lines = [
        f"# Highlights (音量峰值): {source_name}\n",
        f"共 {len(peaks)} 段。按時間排序。\n",
        "---\n",
    ]
    for idx, p in enumerate(peaks, 1):
        # 找出落在這段時間內的逐字稿
        overlapping = [
            s for s in segments
            if s["end"] >= p["start"] and s["start"] <= p["end"]
        ]
        text_block = "\n".join(
            f"  - `[{fmt_ts(s['start'])}]` {to_traditional(s['text'].strip())}"
            for s in overlapping
        ) or "  *(無語音,可能是純笑聲/音效/事故)*"
        lines.append(
            f"### #{idx}  `[{fmt_ts(p['start'])} → {fmt_ts(p['end'])}]` "
            f"+{p['db_above_baseline']:.1f} dB\n\n{text_block}\n"
        )

    # 響度 top 10 索引
    top = sorted(peaks, key=lambda x: x["db_above_baseline"], reverse=True)[:10]
    lines.append("\n---\n## Top 10 最大聲時刻\n")
    for p in top:
        lines.append(
            f"- `{fmt_ts(p['start'])}` +{p['db_above_baseline']:.1f} dB"
        )

    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"      寫出 → {out_md}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path, help="影片或音訊檔")
    ap.add_argument("--model", default="large-v3",
                    choices=["tiny", "base", "small", "medium", "large-v3"])
    ap.add_argument("--lang", default="zh", help="語言代碼,中文=zh、日文=ja")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--peak-db", type=float, default=4.0,
                    help="峰值門檻(基線之上幾 dB),預設 4;命中太多可調高、太少可調低")
    ap.add_argument("--channel", type=Path, default=None,
                    help="頻道設定檔路徑（channels/xxx.yaml），不指定則用預設值")
    args = ap.parse_args()

    channel = load_channel(args.channel)
    if channel["name"]:
        print(f"頻道: {channel['name']} (lang={channel['language']})")

    if not args.input.exists():
        sys.exit(f"找不到檔案: {args.input}")

    # 用 hash 資料夾名,避免中文/特殊字元路徑問題
    out_dir = make_output_dir(args.input)
    print(f"輸出資料夾: {out_dir}")

    # Step 1: 抽音訊
    wav_path = out_dir / "audio.wav"
    if not wav_path.exists():
        extract_audio(args.input, wav_path)
    else:
        print(f"[1/3] 沿用既有音訊 {wav_path.name}")

    # Step 2: ASR(有快取就跳過)
    cache_path = out_dir / "segments.json"
    if cache_path.exists():
        segments = load_segments_cache(cache_path)
    else:
        segments = transcribe(wav_path, args.model, args.lang, args.device,
                              cache_path=cache_path)

    # 寫逐字稿(如果 segments.json 存在但 transcript.md 不在,也會重寫)
    try:
        write_transcript(segments, out_dir / "transcript.md", args.input.name)
    except Exception:
        print(f"[錯誤] 寫入 transcript.md 失敗:")
        traceback.print_exc()

    try:
        write_srt(segments, out_dir / "transcript.srt")
    except Exception:
        print(f"[錯誤] 寫入 transcript.srt 失敗:")
        traceback.print_exc()

    bursts = []
    peaks = []

    try:
        bursts = detect_silence_bursts(segments)
        bursts_path = out_dir / "silence_bursts.json"
        bursts_path.write_text(
            json.dumps(bursts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"      長靜音後爆發: {len(bursts)} 處 → {bursts_path.name}")
    except Exception:
        print(f"[錯誤] 長靜音後爆發偵測失敗:")
        traceback.print_exc()

    # Step 3: 音量峰值
    try:
        peaks = detect_volume_peaks(wav_path, threshold_db_above_baseline=args.peak_db)
        write_highlights(peaks, segments, out_dir / "highlights.md", args.input.name)
    except Exception:
        print(f"[錯誤] 音量分析/寫入 highlights.md 失敗:")
        traceback.print_exc()

    # Step 4: 合併訊號
    try:
        candidates = merge_signals(peaks, bursts, channel=channel)
    except Exception:
        print("[錯誤] 合併訊號失敗:")
        traceback.print_exc()
        candidates = []

    print("\n完成。")


if __name__ == "__main__":
    main()
