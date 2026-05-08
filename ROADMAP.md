# 直播切片標註輔助工具（StreamClip-Tool）

## 目的

把一段直播錄影 → 自動產出**帶時間戳的逐字稿 + 精華點候選清單**，讓人類剪輯者只需要審「機器標出來的可疑亮點」，不用從頭看整場直播。

不做全自動剪輯，只做**標註輔助**——最後挑哪段、怎麼下標題，由人決定。

---

## 輸入 / 輸出

### 輸入
- 直播錄影檔（`.mp4` / `.mkv` / `.ts`），或單純音訊（`.wav` / `.m4a`）
- 可選：頻道設定檔（`channels/xxx.yaml`），定義關鍵字、權重

### 輸出（放在 `output/<hash>/` 底下）
- `transcript.md` — 帶時間戳的逐字稿（繁體中文）
- `transcript.srt` — 字幕檔，可丟剪輯軟體
- `highlights.csv` — 精華點候選清單，含時間區段、命中規則、分數
- `highlights.md` — 同上但人類可讀格式
- `silence_bursts.json` — 長靜音後爆發候選清單
- `segments.json` — whisper 快取（下次不用重跑 ASR）
- `audio.wav` — 抽完的音訊（跑完可刪）
- `source.txt` — 原始檔名記錄

---

## 流程（6 step）

### Step 1：音訊抽取 ✅
- `ffmpeg -i input.mp4 -vn -ac 1 -ar 16000 audio.wav`
- 統一成 16kHz mono

### Step 2：語音辨識（whisper）✅
- 本地 `faster-whisper`（large-v3），CTranslate2 backend
- 輸出每段 segment：`{start, end, text}`
- OpenCC s2twp 簡體轉台灣正體
- segments.json 快取（避免 GPU segfault 導致資料遺失）

### Step 3：說話人分離 ❌ 不做
- ~~pyannote.audio 跑 speaker diarization~~
- 單人直播為主，不需要

### Step 4：精華點偵測（多訊號打分）
對每個 segment 算一個 `highlight_score`，分數高的列入候選。

| 訊號 | 狀態 | 怎麼抓 |
|---|---|---|
| 音量尖峰 | ✅ 完成 | RMS dB，中位數 + 門檻（壓縮音訊效果有限） |
| 長靜音後爆發 | ✅ 完成 | segment gap ≥ 3s，含 min_chars/min_duration 過濾 |
| 關鍵字命中 | ✅ 完成 | channel.yaml 定義梗詞 → 全文比對，命中加分 |
| 重複詞 | ✅ 完成 | 連續/非連續重複偵測（regex + n-gram） |
| 語速突變 | ✅ 完成 | 每段字/秒 vs 全場平均，z-score ≥ 2.0 |

### Step 5：精華區段合併 ✅ 完成
- 各訊號 normalize 0-100 → 乘 channel weights → 合併
- 相鄰候選 gap ≤ merge_gap_sec → 合併成一段
- 每段往前後各擴 padding_sec（避免切到一半）
- 按 score 降序取 top_n，過濾 min_score 以下

### Step 6：產出檔案 ✅ 完成
- ✅ `transcript.md` / `.srt`
- ✅ `highlights.csv`（多訊號合併，utf-8-sig for Excel）
- ✅ `highlights.md`（音量峰值 + 對應台詞）
- ✅ `silence_bursts.json`

---

## 技術選型

| 用途 | 套件 | 備註 |
|---|---|---|
| 音訊處理 | `ffmpeg`（外部）+ `numpy` + `soundfile` | RMS 算音量 |
| ASR | `faster-whisper` | CTranslate2，本地 GPU/CPU |
| 簡繁轉換 | `opencc-python-reimplemented` | s2twp（台灣正體 + 詞彙） |
| 設定檔 | `pyyaml` | channel.yaml + config |
| 輸出表格 | `pandas` | highlights.csv |
| CLI | `argparse` | 內建，不需額外套件 |

**Python 版本：** 3.13  
**GPU：** NVIDIA RTX 3060 Ti 8GB

---

## 資料夾結構

```
StreamClip-Tool/
├── README.md               # 使用說明
├── ROADMAP.md              # 本檔
├── DEV-LOG.md              # 開發進度 & 問題回報
├── TODO.md                 # 待辦清單
├── poc.py                  # 主程式（PoC → MVP 漸進式開發）
├── check.py                # 涵蓋率/空白診斷工具
├── requirements-poc.txt    # Python 依賴
├── channels/
│   ├── _template.yaml      # 頻道設定範本
│   └── reiin.yaml          # 月上零韻頻道設定
├── output/                 # gitignore
└── .venv/                  # gitignore
```

---

## CLI 用法

```bash
# 基本：吃影片，吐逐字稿 + 精華清單
python poc.py input.mp4

# 指定頻道設定（關鍵字權重）
python poc.py input.mp4 --channel channels/myvtuber.yaml

# 調整音量峰值門檻
python poc.py input.mp4 --peak-db 8

# 用 CPU（沒有 NVIDIA GPU 時）
python poc.py input.mp4 --device cpu

# 指定模型大小（VRAM 不夠時降級）
python poc.py input.mp4 --model medium
```

---

## 開發階段

### Phase 1：PoC 驗證 ✅ 完成
ffmpeg 抽音訊 + faster-whisper ASR + 音量峰值偵測 + OpenCC 簡轉繁。
75 分鐘長直播實測通過（1713 段, 82.4% 涵蓋率, RTF=0.14）。

### Phase 2：MVP ✅ 全部完成
在 PoC 基礎上加完整打分系統，產出可用的精華候選清單。

**核心：**
1. ~~SRT 字幕輸出~~ ✅
2. ~~channel.yaml loader~~ ✅
3. ~~長靜音後爆發偵測~~ ✅
4. ~~關鍵字打分~~ ✅
5. ~~合併訊號成候選清單~~ ✅
6. ~~精華區段合併~~ ✅
7. ~~highlights.csv 輸出~~ ✅

**選配（已完成）：**
- ~~重複詞偵測~~ ✅
- ~~語速突變偵測~~ ✅

### Phase 3：進階功能（之後再說）
- `--cut-clips` ffmpeg 預剪精華小段 mp4
- YouTube 彈幕密度分析（live chat replay JSON）
- 本地 LLM（Ollama）給每段下標題草稿
- `.fcpxml` / Premiere XML marker 輸出

### 永遠不做
- ~~pyannote 說話人分離~~（單人直播不需要）
- ~~YAMNet 笑聲偵測~~（文字訊號夠用）
- ~~GUI~~
