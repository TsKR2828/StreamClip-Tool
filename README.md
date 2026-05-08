# StreamClip-Tool — 直播切片標註輔助工具

把一段直播錄影丟進去，自動產出**帶時間戳的繁體中文逐字稿 + 精華點候選清單**。
不做全自動剪輯，只做標註輔助——最後挑哪段、怎麼下標題，由人決定。

## 環境需求

- **Python** 3.11+（目前測試用 3.13）
- **ffmpeg**（系統層級）：`winget install ffmpeg` 或從 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 下載
- **NVIDIA GPU**（建議）：需要 CUDA 12 相容顯卡。目前在 RTX 3060 Ti 8GB 上測試通過

## 安裝

```bash
cd StreamClip-Tool
pip install -r requirements-poc.txt
```

### CUDA DLL 路徑設定（GPU 模式必做）

faster-whisper 需要 cuBLAS 和 cuDNN，pip 裝完後 Windows 找不到 DLL，**每次開新的 PowerShell 視窗都要設一次**：

```powershell
# 1. 裝 CUDA library（只需要第一次）
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12

# 2. 找到 DLL 位置
pip show -f nvidia-cublas-cu12 | Select-String "Location|\.dll"

# 3. 設 PATH（每次開新視窗都要跑）
$nv = "C:\Users\admin\AppData\Local\Programs\Python\Python313\Lib\site-packages\nvidia"
$env:PATH = "$nv\cublas\bin;$nv\cudnn\bin;" + $env:PATH
```

> 💡 如果嫌每次都要設，可以把第 3 步加到 PowerShell profile，或用下方的 `run.ps1`。

### 一鍵啟動腳本（可選）

建立 `run.ps1`：
```powershell
$nv = "C:\Users\admin\AppData\Local\Programs\Python\Python313\Lib\site-packages\nvidia"
$env:PATH = "$nv\cublas\bin;$nv\cudnn\bin;" + $env:PATH
python poc.py $args
```
之後用 `.\run.ps1 "直播.mp4"` 就好。

## 使用方式

```bash
# 基本用法（GPU + large-v3 + 中文）
python poc.py "直播錄影.mp4"

# 指定頻道設定（關鍵字權重）
python poc.py "直播.mp4" --channel channels/reiin.yaml

# 自動切出精華小段 mp4
python poc.py "直播.mp4" --channel channels/reiin.yaml --cut-clips

# 加入 YouTube 彈幕密度訊號
python poc.py "直播.mp4" --chat-json chat.json

# 用 Ollama 產生標題草稿（需本機執行 ollama serve）
python poc.py "直播.mp4" --titles --ollama-model llama3

# 調整音量峰值門檻
python poc.py "直播.mp4" --peak-db 3     # 更敏感
python poc.py "直播.mp4" --peak-db 8     # 只抓最誇張的

# CPU 模式（沒有 NVIDIA GPU 時）
python poc.py "直播.mp4" --device cpu --model medium

# 日文內容
python poc.py "配信.mp4" --lang ja
```

## 輸出結構

```
output/<hash>_<檔名前20字>/
├── source.txt            # 原始檔名記錄
├── audio.wav             # 抽出的 16kHz mono 音訊（可刪）
├── segments.json         # whisper 辨識快取（下次秒讀）
├── transcript.md         # 帶時間戳的繁體中文逐字稿
├── transcript.srt        # SRT 字幕檔（可直接匯入剪輯軟體）
├── highlights.csv        # 精華候選清單（多訊號合併，Excel 可開）
├── highlights.md         # 音量峰值候選 + 對應台詞
├── silence_bursts.json   # 長靜音後爆發候選清單
├── markers.edl           # EDL 剪輯標記（可匯入 Premiere / DaVinci Resolve）
├── chapters.txt          # YouTube 章節時間軸（貼到影片說明欄）
└── clips/                # 精華小段 mp4（--cut-clips 時產出）
    ├── clip_01_*.mp4
    ├── clip_02_*.mp4
    └── ...
```

### 快取機制

- `audio.wav` 存在就跳過 ffmpeg
- `segments.json` 存在就跳過 whisper（省 10+ 分鐘）
- 想重跑 whisper：刪掉 `segments.json` 再跑

## 頻道設定

每個 VTuber / 頻道可以自訂 `channels/<name>.yaml`，定義關鍵字和各訊號的權重比例：

```yaml
name: "頻道名"
language: zh

keywords:
  梗詞A: 10      # whisper 實際輸出的文字（不是你想說的）
  笑死: 20
  神回: 30

weights:
  volume_spike: 15      # 壓縮音訊降權
  keyword_hit: 45       # 主力訊號
  silence_burst: 25
  repeated_word: 10
  speech_rate_change: 5

highlight:
  top_n: 30
  min_score: 5
  merge_gap_sec: 5.0
  padding_sec: 3.0
```

不指定 `--channel` 時使用內建預設值。範本見 `channels/_template.yaml`。

## 精華偵測訊號

| 訊號 | 說明 | 適用場景 |
|------|------|---------|
| 音量尖峰 | RMS 超過中位數 + 門檻 dB | 吶喊、大笑（音訊未壓縮時效果佳） |
| 長靜音後爆發 | 前一段結束到這段開始 ≥ 3s | 看彈幕後突然開口、事故後沉默 |
| 關鍵字命中 | channel.yaml 定義的梗詞 | 口頭禪、特定梗、招呼語 |
| 重複詞 | 同一詞在 segment 內重複 3+ 次 | 興奮連呼「犯罪犯罪」「經典經典」 |
| 語速突變 | 字/秒偏離全場平均 ≥ 2σ | 快語速=興奮、慢語速=讀彈幕/強調 |

各訊號先在自身類型內 normalize 到 0-100，再乘以 channel weights，最後合併相鄰區段。

## 效能參考（RTX 3060 Ti 8GB）

| 模型 | VRAM | 1 小時音訊耗時 | 中文品質 |
|------|------|----------------|----------|
| large-v3 | ~5GB | ~12 分鐘 (RTF≈0.15) | 最好 |
| medium | ~2.5GB | ~6 分鐘 | 堪用 |
| small | ~1.5GB | ~3 分鐘 | 口語常錯 |

## 目前限制

- 音量偵測對**經過動態壓縮**的音訊效果差（OBS 壓縮器、麥克風內建壓縮），此時應調高 keyword_hit 權重
- 關鍵字必須填 whisper **實際輸出的文字**（例如「Alohaございます」→ 聽成「阿羅哈」）
- 詳見 [ROADMAP.md](ROADMAP.md) 及 [DEV-LOG.md](DEV-LOG.md)
