# 直播切片標註輔助工具（StreamClip-Tool）

## 目的

把一段直播錄影 → 自動產出**帶時間戳的逐字稿 + 精華點候選清單**，讓人類剪輯者只需要審「機器標出來的可疑亮點」，不用從頭看整場直播。

不做全自動剪輯，只做**標註輔助**——最後挑哪段、怎麼下標題，由人決定。

---

## 輸入 / 輸出

### 輸入
- 直播錄影檔（`.mp4` / `.mkv` / `.ts`），或單純音訊（`.wav` / `.m4a`）
- 可選：頻道設定檔（`channel.yaml`），定義 VTuber 名字、常用梗、關鍵字權重

### 輸出（放在 `output/<影片檔名>/` 底下）
- `transcript.md` — 帶時間戳的逐字稿，含說話人標籤
- `transcript.srt` — 字幕檔，可丟剪輯軟體
- `highlights.md` — 精華點候選清單，每筆含時間區段、命中規則、原文片段
- `highlights.csv` — 同上但表格化，方便排序篩選
- `clips/` — （可選）直接用 ffmpeg 預剪好的小段 `.mp4`，方便快速預覽

---

## 流程（6 step）

### Step 1：音訊抽取
- `ffmpeg -i input.mp4 -vn -ac 1 -ar 16000 audio.wav`
- 統一成 16kHz mono，給後面模型吃

### Step 2：語音辨識（whisper）
- 本地跑 `faster-whisper`（比官方 whisper 快 3–5 倍，吃 VRAM 少）
- 模型大小：先用 `large-v3`，VRAM 不夠退 `medium`
- 輸出每段 segment：`{start, end, text}`
- 語言：日文為主，可在 channel.yaml 指定

### Step 3：說話人分離（pyannote）
- `pyannote.audio` 跑 speaker diarization
- 輸出每段：`{start, end, speaker_id}`（speaker_0、speaker_1…）
- 跟 Step 2 的逐字稿時間戳對齊 → 每句話加上 speaker 標籤
- 多人聯動直播時特別有用；單人直播可直接跳過

### Step 4：精華點偵測（規則式打分）
對每個 segment 算一個 `highlight_score`，分數高的列入候選。

**訊號來源：**

| 訊號 | 怎麼抓 | 為什麼是亮點 |
|---|---|---|
| 音量尖峰 | librosa 算 RMS，找局部最大 | 大笑、大叫、唱歌 |
| 笑聲 | 文字含「www」「草」「ｗ」「笑」/ 音訊用 YAMNet 分類 | 觀眾覺得好笑 = 可能也好笑 |
| 關鍵字命中 | channel.yaml 定義（例：「歌枠」「初見」「事故」「神回」） | 自訂亮點主題 |
| 語速突變 | 每分鐘字數變化 > 閾值 | 情緒激動或冷場後爆發 |
| 長靜音後爆發 | 前 N 秒幾乎無聲、接著音量飆 | 經典「停頓 → 爆笑」節奏 |
| 重複詞 | 同一個詞短時間出現 3+ 次 | 角色玩梗、復讀 |

**打分：**
- 每個訊號給權重（可在 `config.yaml` 調）
- 加總後 normalize 成 0–100
- 取 top N（預設 30）或 score > 閾值的進候選

### Step 5：精華區段合併
- 相鄰且都高分的 segment 合併成一段
- 每段往前後各擴 3 秒 padding（避免切到一半）
- 同一個亮點被多個訊號命中 → 標註全部命中規則，不重複列

### Step 6：產出檔案
- 寫 `transcript.md` / `.srt`
- 寫 `highlights.md` / `.csv`，按分數排序
- 若啟用 `--cut-clips`，呼叫 ffmpeg 把每段切成獨立 mp4 放 `clips/`

---

## 技術選型

| 用途 | 套件 | 備註 |
|---|---|---|
| 音訊處理 | `ffmpeg`（外部）+ `librosa` | librosa 算 RMS、靜音偵測 |
| ASR | `faster-whisper` | CTranslate2 backend，本地 GPU/CPU 都能跑 |
| Diarization | `pyannote.audio` 3.x | 需要 HuggingFace token（一次設定） |
| 音訊分類（笑聲） | `tensorflow-hub` 的 YAMNet | 可選，第一版可只靠文字判斷 |
| 設定檔 | YAML（`pyyaml`） | channel.yaml + config.yaml |
| 輸出 Excel/CSV | `pandas` | 跟 OCR-Tool 一致 |
| CLI | `typer` 或 `argparse` | 看哪個順手 |

**Python 版本：** 3.11（跟現有 OCR-Tool 對齊）

---

## 資料夾結構

```
StreamClip-Tool/
├── PLAN.md                  # 本檔
├── README.md                # 安裝/使用說明（之後寫）
├── requirements.txt
├── config.yaml              # 全域預設（權重、模型大小）
├── channels/
│   ├── _template.yaml       # 頻道設定範本
│   └── example.yaml
├── src/
│   ├── __init__.py
│   ├── cli.py               # 入口點
│   ├── extract_audio.py     # Step 1
│   ├── transcribe.py        # Step 2
│   ├── diarize.py           # Step 3
│   ├── score.py             # Step 4
│   ├── merge.py             # Step 5
│   └── render.py            # Step 6
├── output/                  # gitignore
└── tests/
    └── fixtures/            # 短測試片段
```

---

## CLI 設計

```bash
# 最小：吃影片，吐逐字稿 + 精華清單
python -m src.cli run input.mp4

# 指定頻道設定（決定關鍵字權重、語言）
python -m src.cli run input.mp4 --channel channels/myvtuber.yaml

# 連同預剪小段一起輸出
python -m src.cli run input.mp4 --cut-clips

# 只跑某幾個 step（debug 用）
python -m src.cli run input.mp4 --only transcribe,score

# 已有 transcript.json，跳過 ASR 直接重打分
python -m src.cli rescore output/foo/
```

---

## channel.yaml 範例

```yaml
name: "月月Ch."
language: ja
speakers:
  - id: speaker_0
    name: "月月"
  - id: speaker_1
    name: "客串"

# 關鍵字 → 加分權重（命中一次加多少）
keywords:
  神回: 30
  事故: 25
  初見: 15
  歌枠: 20
  かわいい: 5

# 笑聲訊號權重（0–100）
weights:
  volume_spike: 20
  laughter: 25
  keyword_hit: 30
  speech_rate_change: 10
  silence_then_burst: 10
  repeated_word: 5
```

---

## MVP 範圍（第一版只做這些）

第一版**砍掉**以下，先把核心跑通：
- ❌ pyannote 說話人分離（單人直播不需要）
- ❌ YAMNet 笑聲偵測（先靠文字「www / 草」判斷）
- ❌ `--cut-clips` 自動剪輯
- ❌ GUI

**MVP 只做：**
1. ffmpeg 抽音訊
2. faster-whisper 出逐字稿
3. 文字關鍵字 + 音量尖峰兩個訊號打分
4. 輸出 `transcript.md` + `highlights.csv`

驗證：對一場已知有梗的直播跑一次，看 highlights top 10 命中率。

---

## 之後可以加的

- 輸出 `.fcpxml` / Premiere XML，直接開進剪輯軟體就有 marker
- 整合 chat（YouTube live chat replay JSON），把彈幕密度也當訊號
- 多場直播跨集統計：哪個關鍵字長期命中率高 → 自動回填 channel.yaml 權重
- 用本地 LLM（Ollama）給每個精華段下一句日文標題草稿

---

## 風險 / 待確認

1. **VRAM**：faster-whisper large-v3 + pyannote 同時跑大概要 8GB 以上 VRAM；機器跑不動就降模型或分階段跑
2. **pyannote token**：需要去 HuggingFace 接受授權條款，首次設定要手動
3. **日文 ASR 標點**：whisper 的日文輸出標點不穩，逐字稿可能要後處理（句號補齊）
4. **長片記憶體**：3 小時直播一次吃進去可能爆，需要分塊處理（每 30 分鐘一段）

---

## 下一步

確認 MVP 範圍後，我可以：
- 直接開 `requirements.txt` + `src/cli.py` 骨架
- 或先寫一個 50 行的 PoC，只跑 ffmpeg + whisper，確認本機能跑起來再擴
