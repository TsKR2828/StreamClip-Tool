"""
StreamClip-Tool: 直播切片標註輔助工具

吃一個影片/音訊檔,跑 faster-whisper ASR + 五訊號精華偵測,
吐帶時間戳的逐字稿 + 精華候選清單 + 可選自動切片。

用法:
    python poc.py <input.mp4>
    python poc.py <input.mp4> --channel channels/reiin.yaml
    python poc.py <input.mp4> --cut-clips              # 自動切精華小段 mp4
    python poc.py <input.mp4> --chat-json chat.json     # YouTube 彈幕密度訊號
    python poc.py <input.mp4> --titles                  # Ollama 標題草稿
    python poc.py <input.mp4> --peak-db 8               # 調高音量門檻
    python poc.py <input.mp4> --device cpu

輸出:
    output/<hash>/transcript.md         逐字稿
    output/<hash>/transcript.srt        字幕檔（SRT 格式,可丟剪輯軟體）
    output/<hash>/highlights.csv        精華候選清單（多訊號合併,Excel 可開）
    output/<hash>/highlights.md         音量峰值 + 對應台詞
    output/<hash>/silence_bursts.json   長靜音後爆發候選清單
    output/<hash>/segments.json         whisper 快取(下次不用重跑)
    output/<hash>/audio.wav             抽完的音訊(跑完可刪)
    output/<hash>/source.txt            原始檔名記錄
    output/<hash>/markers.edl           EDL 剪輯標記（--cut-clips 時產出）
    output/<hash>/chapters.txt          YouTube 章節時間軸（--cut-clips 時產出）
    output/<hash>/clips/                精華小段 mp4（--cut-clips 時產出）
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
            "chat_density": 30,
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


def score_keywords(segments: list, channel: dict = None) -> list:
    """關鍵字打分：對每個 segment 的文字比對 channel.yaml 的 keywords。

    每個 keyword 命中一次加對應的權重分。同一 segment 可命中多個 keyword。
    同一 keyword 出現多次會乘以次數（例如「哈哈哈哈哈哈」含 2 次「哈哈哈」→ ×2）。

    回傳: list of dict {start, end, score, reasons}
    """
    if channel is None:
        channel = load_channel(None)
    keywords = channel.get("keywords", {})
    if not keywords:
        return []

    hits = []
    for seg in segments:
        text = to_traditional(seg["text"].strip())
        seg_score = 0
        matched = []
        for kw, weight in keywords.items():
            count = text.count(kw)
            if count > 0:
                seg_score += weight * count
                if count > 1:
                    matched.append(f"kw:{kw}×{count}")
                else:
                    matched.append(f"kw:{kw}")
        if seg_score > 0:
            hits.append({
                "start": seg["start"],
                "end": seg["end"],
                "score": seg_score,
                "reasons": matched,
            })

    if hits:
        total_kw = sum(h["score"] for h in hits)
        print(f"      關鍵字命中: {len(hits)} 段, "
              f"總權重 {total_kw}, "
              f"最高 {max(h['score'] for h in hits)}")
    else:
        print("      關鍵字命中: 0 段")
    return hits


def detect_repeated_words(
    segments: list,
    min_repeats: int = 3,
) -> list:
    """重複詞偵測：同一詞在 segment 內重複出現。

    偵測兩類重複：
    1. 連續重複（regex）：「哈哈哈」「不要不要不要」「欸欸欸欸」
    2. 非連續重複（n-gram 計數）：「好吃...真的好吃...超好吃」

    中文無空格分詞，用 2 字元滑窗做 n-gram。
    常見虛詞（的、了、是…）會被過濾。

    回傳: list of dict {start, end, score, reasons}
    """
    import re

    # 常見虛詞 / 標點，不計入重複
    stopchars = set("的了是在不我你他她它們有這那個都也就要會可以"
                    "，。！？、…～．·. ")

    hits = []
    for seg in segments:
        text = to_traditional(seg["text"].strip())
        if len(text) < 6:
            continue

        found = []

        # 1. 連續單字重複 3+：哈哈哈、對對對、欸欸欸
        for m in re.finditer(r"(.)\1{2,}", text):
            char = m.group(1)
            if char in stopchars:
                continue
            count = len(m.group(0))
            found.append((f"{char}×{count}", count * 3))

        # 2. 連續雙字重複 2+：不要不要、好吃好吃好吃
        for m in re.finditer(r"(.{2})\1{1,}", text):
            word = m.group(1)
            if len(set(word)) <= 1:          # 跟單字重複重疊，跳過
                continue
            count = len(m.group(0)) // len(word)
            if count >= 2:
                found.append((f"{word}×{count}", count * 5))

        # 3. 非連續 2-gram 出現 3+ 次（抓散落的重複）
        if len(text) >= 10:
            ngram_counts: dict[str, int] = {}
            for i in range(len(text) - 1):
                gram = text[i : i + 2]
                if any(c in stopchars for c in gram):
                    continue
                ngram_counts[gram] = ngram_counts.get(gram, 0) + 1
            for word, count in ngram_counts.items():
                if count >= min_repeats:
                    # 避免跟連續重複重複計分
                    already = any(word in f[0] for f in found)
                    if not already:
                        found.append((f"{word}×{count}", count * 4))

        if found:
            best = max(found, key=lambda x: x[1])
            hits.append({
                "start": seg["start"],
                "end": seg["end"],
                "score": best[1],
                "reasons": [f"repeat:{best[0]}"],
            })

    if hits:
        print(f"      重複詞命中: {len(hits)} 段, "
              f"最高 {max(h['score'] for h in hits)}")
    else:
        print("      重複詞命中: 0 段")
    return hits


def detect_speech_rate_changes(
    segments: list,
    z_threshold: float = 2.0,
    min_chars: int = 6,
) -> list:
    """語速突變偵測：每段字/秒 vs 全場平均，偏離超過 z_threshold 標準差。

    語速過快 → 興奮 / 緊張
    語速過慢 → 強調 / 沉思 / 讀彈幕

    回傳: list of dict {start, end, score, reasons}
    """
    rates: list[tuple[dict, float]] = []
    for seg in segments:
        text = to_traditional(seg["text"].strip())
        duration = seg["end"] - seg["start"]
        if duration < 0.5 or len(text) < min_chars:
            continue
        rates.append((seg, len(text) / duration))

    if len(rates) < 10:
        return []

    all_rates = np.array([r for _, r in rates])
    mean_rate = float(np.mean(all_rates))
    std_rate = float(np.std(all_rates))

    if std_rate < 0.5:
        print("      語速突變: 標準差太小，跳過")
        return []

    hits = []
    for seg, rate in rates:
        z = (rate - mean_rate) / std_rate
        if abs(z) >= z_threshold:
            direction = "快" if z > 0 else "慢"
            hits.append({
                "start": seg["start"],
                "end": seg["end"],
                "score": round(abs(z) * 5, 1),
                "reasons": [f"pace:{direction}({rate:.1f}c/s,avg={mean_rate:.1f})"],
            })

    if hits:
        print(f"      語速突變: {len(hits)} 段 "
              f"(均速 {mean_rate:.1f} 字/秒, σ={std_rate:.1f})")
    else:
        print(f"      語速突變: 0 段 (均速 {mean_rate:.1f} 字/秒)")
    return hits


def analyze_chat_density(
    chat_path: Path,
    window_sec: float = 10.0,
    z_threshold: float = 2.0,
) -> list:
    """分析 YouTube 聊天室訊息密度，找出彈幕密集的時間點。

    支援格式：
    - JSON array（每個物件需含 time_in_seconds 或 time_text 欄位）
    - JSONL（每行一個 JSON 物件）
    兩種都是 chat_downloader / yt-dlp 的常見輸出格式。

    流程：
    1. 解析時間戳 → 切 window_sec 窗口計算每窗訊息數
    2. 找 z-score ≥ z_threshold 的窗口（彈幕密度尖峰）

    回傳: list of dict {start, end, score, reasons}
    """
    raw = chat_path.read_text(encoding="utf-8")

    # 嘗試 JSON array → JSONL
    messages = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            messages = data
    except json.JSONDecodeError:
        # JSONL: 每行一個 JSON
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not messages:
        print("      彈幕分析: 解析失敗或檔案為空")
        return []

    # 取得每則訊息的時間（秒）
    times = []
    for msg in messages:
        t = msg.get("time_in_seconds")
        if t is None:
            # fallback: 嘗試其他常見欄位
            t = msg.get("timestamp") or msg.get("time") or msg.get("offset")
        if t is not None:
            try:
                times.append(float(t))
            except (ValueError, TypeError):
                continue
    if not times:
        # 嘗試從 time_text 解析 "MM:SS" 格式
        for msg in messages:
            tt = msg.get("time_text", "")
            parts = tt.replace("−", "-").split(":")
            try:
                if len(parts) == 2:
                    times.append(int(parts[0]) * 60 + int(parts[1]))
                elif len(parts) == 3:
                    times.append(
                        int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    )
            except ValueError:
                continue

    if len(times) < 10:
        print(f"      彈幕分析: 訊息太少 ({len(times)} 則)，跳過")
        return []

    times.sort()
    max_time = times[-1]
    n_windows = int(max_time / window_sec) + 1

    # 計算每窗訊息數
    counts = np.zeros(n_windows)
    for t in times:
        idx = min(int(t / window_sec), n_windows - 1)
        counts[idx] += 1

    mean_c = float(np.mean(counts))
    std_c = float(np.std(counts))
    if std_c < 1.0:
        print(f"      彈幕分析: 密度太均勻 (σ={std_c:.1f})，跳過")
        return []

    hits = []
    for i in range(n_windows):
        z = (counts[i] - mean_c) / std_c
        if z >= z_threshold:
            hits.append({
                "start": i * window_sec,
                "end": (i + 1) * window_sec,
                "score": round(z * counts[i], 1),
                "reasons": [f"chat({int(counts[i])}msg/{window_sec:.0f}s)"],
            })

    print(f"      彈幕分析: {len(times)} 則訊息, "
          f"{len(hits)} 個密度尖峰 "
          f"(均 {mean_c:.1f} 則/窗, σ={std_c:.1f})")
    return hits


def merge_signals(
    volume_peaks: list,
    silence_bursts: list,
    keyword_hits: list = None,
    repeated_word_hits: list = None,
    speech_rate_hits: list = None,
    chat_density_hits: list = None,
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

    # — 關鍵字命中 —
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

    # — 重複詞 —
    if repeated_word_hits:
        max_rw = max((rh["score"] for rh in repeated_word_hits), default=0)
        if max_rw > 0:
            w = weights.get("repeated_word", 10)
            for rh in repeated_word_hits:
                raw = rh["score"] / max_rw * 100
                candidates.append({
                    "start": rh["start"],
                    "end": rh["end"],
                    "score": round(raw * w / 100, 1),
                    "reasons": rh.get("reasons", ["repeat"]),
                })

    # — 語速突變 —
    if speech_rate_hits:
        max_sr = max((sh["score"] for sh in speech_rate_hits), default=0)
        if max_sr > 0:
            w = weights.get("speech_rate_change", 5)
            for sh in speech_rate_hits:
                raw = sh["score"] / max_sr * 100
                candidates.append({
                    "start": sh["start"],
                    "end": sh["end"],
                    "score": round(raw * w / 100, 1),
                    "reasons": sh.get("reasons", ["pace"]),
                })

    # — 彈幕密度 —
    if chat_density_hits:
        max_cd = max((ch["score"] for ch in chat_density_hits), default=0)
        if max_cd > 0:
            w = weights.get("chat_density", 30)
            for ch in chat_density_hits:
                raw = ch["score"] / max_cd * 100
                candidates.append({
                    "start": ch["start"],
                    "end": ch["end"],
                    "score": round(raw * w / 100, 1),
                    "reasons": ch.get("reasons", ["chat"]),
                })

    candidates.sort(key=lambda c: c["start"])
    parts = [
        f"volume={len(volume_peaks or [])}",
        f"silence={len(silence_bursts or [])}",
        f"keyword={len(keyword_hits or [])}",
        f"repeat={len(repeated_word_hits or [])}",
        f"pace={len(speech_rate_hits or [])}",
    ]
    if chat_density_hits:
        parts.append(f"chat={len(chat_density_hits)}")
    print(f"      訊號合併: {len(candidates)} 個候選 ({', '.join(parts)})")
    return candidates


def merge_highlights(
    candidates: list,
    segments: list,
    channel: dict = None,
) -> list:
    """合併相鄰/重疊的候選區段，產出最終精華清單。

    1. 按 start 排序
    2. 相鄰候選 gap ≤ merge_gap_sec → 合併成一段
    3. 合併後 score 取最高（同段多訊號 → reasons 合併）
    4. 前後各擴 padding_sec
    5. 附上對應的逐字稿片段
    6. 按 score 降序排列，取 top_n

    回傳: list of dict {rank, start, end, duration, score, reasons, transcript}
    """
    if channel is None:
        channel = load_channel(None)
    cfg = channel["highlight"]
    merge_gap = cfg.get("merge_gap_sec", 5.0)
    padding = cfg.get("padding_sec", 3.0)
    top_n = cfg.get("top_n", 30)
    min_score = cfg.get("min_score", 10)

    if not candidates:
        return []

    # 1. 按 start 排序
    sorted_cands = sorted(candidates, key=lambda c: c["start"])

    # 2. 合併相鄰/重疊候選
    merged = []
    cur = {
        "start": sorted_cands[0]["start"],
        "end": sorted_cands[0]["end"],
        "score": sorted_cands[0]["score"],
        "reasons": list(sorted_cands[0]["reasons"]),
    }
    for c in sorted_cands[1:]:
        if c["start"] <= cur["end"] + merge_gap:
            # 合併：擴展 end、累加 score、合併 reasons
            cur["end"] = max(cur["end"], c["end"])
            cur["score"] = round(cur["score"] + c["score"], 1)
            for r in c["reasons"]:
                if r not in cur["reasons"]:
                    cur["reasons"].append(r)
        else:
            merged.append(cur)
            cur = {
                "start": c["start"],
                "end": c["end"],
                "score": c["score"],
                "reasons": list(c["reasons"]),
            }
    merged.append(cur)

    # 3. 加 padding + 計算 duration
    for m in merged:
        m["start"] = max(0, m["start"] - padding)
        m["end"] = m["end"] + padding
        m["duration"] = round(m["end"] - m["start"], 2)

    # 4. 篩選 + 排序
    merged = [m for m in merged if m["score"] >= min_score]
    merged.sort(key=lambda m: m["score"], reverse=True)
    merged = merged[:top_n]

    # 5. 附上逐字稿片段
    for m in merged:
        overlapping = [
            s for s in segments
            if s["end"] >= m["start"] and s["start"] <= m["end"]
        ]
        m["transcript"] = " ".join(
            to_traditional(s["text"].strip()) for s in overlapping
        )

    # 6. 加 rank
    for i, m in enumerate(merged, 1):
        m["rank"] = i
        m["reasons"] = ", ".join(m["reasons"])

    print(f"      區段合併: {len(sorted_cands)} 候選 → {len(merged)} 段精華")
    return merged


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


def write_highlights_csv(highlights: list, out_csv: Path) -> None:
    """把合併後的精華清單寫成 CSV，方便排序篩選。"""
    import pandas as pd

    if not highlights:
        print("      highlights.csv: 無精華段，跳過")
        return

    rows = []
    for h in highlights:
        row = {
            "rank": h["rank"],
            "start": fmt_ts(h["start"]),
            "end": fmt_ts(h["end"]),
            "duration_sec": h["duration"],
            "score": h["score"],
            "reasons": h["reasons"],
        }
        if "title" in h:
            row["title"] = h["title"]
        row["transcript"] = h["transcript"][:200]  # 截斷避免 CSV 太寬
        rows.append(row)

    df = pd.DataFrame(rows)
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")  # Excel 開 CSV 需要 BOM
    print(f"      寫出 → {out_csv} ({len(df)} 筆)")


# ──────────────────────────────────────────────
# Phase 3: 進階功能
# ──────────────────────────────────────────────

def cut_clips(
    input_path: Path,
    highlights: list,
    out_dir: Path,
) -> None:
    """用 ffmpeg 把每個精華段落切成獨立的 mp4。

    使用 -c copy（不重新編碼）以求速度。
    注意：因為只在 keyframe 切，起點可能偏移幾秒，這是 stream copy 的正常行為。
    如果需要精確到幀的切割，請手動加 -c:v libx264 重新編碼。
    """
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(exist_ok=True)

    ok = 0
    for h in highlights:
        rank = h["rank"]
        tag = fmt_ts(h["start"]).replace(":", "").replace(".", "")
        out_file = clips_dir / f"clip_{rank:02d}_{tag}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(h["start"]),
            "-i", str(input_path),
            "-t", str(h["duration"]),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(out_file),
        ]
        try:
            subprocess.run(
                cmd, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            ok += 1
        except subprocess.CalledProcessError:
            print(f"      [警告] clip #{rank} 切割失敗，跳過")
    print(f"      切片完成: {ok}/{len(highlights)} 個 → {clips_dir}")


def generate_titles(
    highlights: list,
    model: str = "llama3",
    base_url: str = "http://localhost:11434",
) -> list:
    """用本地 Ollama 為每個精華段落產生標題草稿。

    需要 Ollama 在本機執行中（ollama serve）。
    標題會直接寫入 highlight dict 的 "title" 欄位。
    """
    import urllib.request
    import urllib.error

    url = f"{base_url}/api/generate"
    ok = 0
    for h in highlights:
        transcript = h.get("transcript", "")[:300]
        if not transcript.strip():
            h["title"] = ""
            continue
        prompt = (
            "你是一個直播切片標題產生器。\n"
            "請根據以下直播逐字稿片段，用繁體中文寫一個簡短、有趣、"
            "吸引人點擊的標題（15 字以內，不要加引號）。\n\n"
            f"逐字稿：\n{transcript}\n\n標題："
        )
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                title = result.get("response", "").strip().split("\n")[0]
                h["title"] = title
                ok += 1
        except (urllib.error.URLError, TimeoutError) as e:
            if ok == 0:
                print(f"      [錯誤] Ollama 連線失敗 ({e})，跳過標題產生")
                return highlights
            h["title"] = ""

    print(f"      標題草稿: {ok}/{len(highlights)} 個 (model={model})")
    return highlights


def write_markers(
    highlights: list,
    out_dir: Path,
    source_name: str = "",
    fps: int = 30,
) -> None:
    """輸出精華標記檔案，供剪輯軟體匯入。

    產出：
    - markers.edl — CMX 3600 EDL（Premiere / DaVinci Resolve / FCPX 可匯入）
    - chapters.txt — YouTube 章節格式（貼到影片說明欄）
    """
    if not highlights:
        return

    # 按時間排序（不是分數）
    by_time = sorted(highlights, key=lambda h: h["start"])

    # --- EDL ---
    def tc(sec: float) -> str:
        """秒 → SMPTE timecode HH:MM:SS:FF"""
        total_frames = int(round(sec * fps))
        ff = total_frames % fps
        total_sec = total_frames // fps
        ss = total_sec % 60
        total_sec //= 60
        mm = total_sec % 60
        hh = total_sec // 60
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

    edl_lines = [
        f"TITLE: {source_name or 'StreamClip Highlights'}",
        f"FCM: NON-DROP FRAME",
        "",
    ]
    for i, h in enumerate(by_time, 1):
        src_in = tc(h["start"])
        src_out = tc(h["end"])
        title = h.get("title", "")
        label = title if title else f"Highlight #{h['rank']} ({h['score']})"
        edl_lines.append(
            f"{i:03d}  001      V     C        "
            f"{src_in} {src_out} {src_in} {src_out}"
        )
        edl_lines.append(f"* FROM CLIP NAME: {label}")
        edl_lines.append("")

    edl_path = out_dir / "markers.edl"
    edl_path.write_text("\n".join(edl_lines), encoding="utf-8")
    print(f"      寫出 → {edl_path}")

    # --- YouTube chapters ---
    chap_lines = []
    for h in by_time:
        total_sec = int(h["start"])
        mm = total_sec // 60
        ss = total_sec % 60
        title = h.get("title", "")
        label = title if title else f"Highlight #{h['rank']}"
        chap_lines.append(f"{mm:02d}:{ss:02d} {label}")

    chap_path = out_dir / "chapters.txt"
    chap_path.write_text("\n".join(chap_lines), encoding="utf-8")
    print(f"      寫出 → {chap_path}")


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
    ap.add_argument("--cut-clips", action="store_true",
                    help="自動切出精華小段 mp4（需要 ffmpeg）")
    ap.add_argument("--chat-json", type=Path, default=None,
                    help="YouTube 聊天室 JSON 檔路徑（chat_downloader 格式）")
    ap.add_argument("--titles", action="store_true",
                    help="用本地 Ollama 為每段精華產生標題草稿")
    ap.add_argument("--ollama-model", default="llama3",
                    help="Ollama 模型名稱（預設 llama3）")
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

    # Step 4a: 關鍵字打分
    keyword_hits = []
    try:
        keyword_hits = score_keywords(segments, channel=channel)
    except Exception:
        print("[錯誤] 關鍵字打分失敗:")
        traceback.print_exc()

    # Step 4b: 重複詞偵測
    repeated_hits = []
    try:
        repeated_hits = detect_repeated_words(segments)
    except Exception:
        print("[錯誤] 重複詞偵測失敗:")
        traceback.print_exc()

    # Step 4c: 語速突變偵測
    speech_rate_hits = []
    try:
        speech_rate_hits = detect_speech_rate_changes(segments)
    except Exception:
        print("[錯誤] 語速突變偵測失敗:")
        traceback.print_exc()

    # Step 4d: 彈幕密度分析（可選）
    chat_hits = []
    if args.chat_json:
        try:
            if not args.chat_json.exists():
                print(f"[警告] 找不到聊天室檔案: {args.chat_json}")
            else:
                chat_hits = analyze_chat_density(args.chat_json)
        except Exception:
            print("[錯誤] 彈幕密度分析失敗:")
            traceback.print_exc()

    # Step 4e: 合併訊號
    try:
        candidates = merge_signals(peaks, bursts, keyword_hits=keyword_hits,
                                   repeated_word_hits=repeated_hits,
                                   speech_rate_hits=speech_rate_hits,
                                   chat_density_hits=chat_hits,
                                   channel=channel)
    except Exception:
        print("[錯誤] 合併訊號失敗:")
        traceback.print_exc()
        candidates = []

    # Step 5: 區段合併
    highlights = []
    try:
        highlights = merge_highlights(candidates, segments, channel=channel)
    except Exception:
        print("[錯誤] 區段合併失敗:")
        traceback.print_exc()

    # Step 6a: Ollama 標題草稿（可選）
    if args.titles and highlights:
        try:
            highlights = generate_titles(highlights, model=args.ollama_model)
        except Exception:
            print("[錯誤] 標題產生失敗:")
            traceback.print_exc()

    # Step 6b: 輸出 highlights.csv
    try:
        write_highlights_csv(highlights, out_dir / "highlights.csv")
    except Exception:
        print("[錯誤] 寫入 highlights.csv 失敗:")
        traceback.print_exc()

    # Step 6c: 剪輯標記檔（EDL + YouTube chapters）
    if highlights:
        try:
            write_markers(highlights, out_dir, source_name=args.input.name)
        except Exception:
            print("[錯誤] 寫入 markers 失敗:")
            traceback.print_exc()

    # Step 7: 自動切片（可選）
    if args.cut_clips and highlights:
        try:
            cut_clips(args.input, highlights, out_dir)
        except Exception:
            print("[錯誤] 切片失敗:")
            traceback.print_exc()

    print("\n完成。")


if __name__ == "__main__":
    main()
