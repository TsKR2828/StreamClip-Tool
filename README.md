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

# 指定模型大小（VRAM 不夠或想要更快）
python poc.py "直播.mp4" --model medium

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
├── source.txt        # 原始檔名記錄
├── audio.wav         # 抽出的 16kHz mono 音訊（可刪）
├── segments.json     # whisper 辨識快取（下次秒讀）
├── transcript.md     # 帶時間戳的繁體中文逐字稿
└── highlights.md     # 音量峰值候選 + 對應台詞
```

### 快取機制

- `audio.wav` 存在就跳過 ffmpeg
- `segments.json` 存在就跳過 whisper（省 10+ 分鐘）
- 想重跑 whisper：刪掉 `segments.json` 再跑

## 效能參考（RTX 3060 Ti 8GB）

| 模型 | VRAM | 1 小時音訊耗時 | 中文品質 |
|------|------|----------------|----------|
| large-v3 | ~5GB | ~12 分鐘 (RTF≈0.15) | 最好 |
| medium | ~2.5GB | ~6 分鐘 | 堪用 |
| small | ~1.5GB | ~3 分鐘 | 口語常錯 |

## 目前限制

- 音量偵測對**經過動態壓縮**的音訊效果差（OBS 壓縮器、麥克風內建壓縮）
- 詳見 [ROADMAP.md](ROADMAP.md) 及 [DEV-LOG.md](DEV-LOG.md)
