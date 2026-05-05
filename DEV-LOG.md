# DEV-LOG：開發進度 & 問題回報

## 開發進度

### Phase 1：PoC 驗證（進行中）

| 項目 | 狀態 | 備註 |
|------|------|------|
| ffmpeg 抽音訊 | ✅ 完成 | 16kHz mono wav |
| faster-whisper 語音辨識 | ✅ 完成 | large-v3, RTF≈0.15 |
| 簡體 → 繁體轉換 | ✅ 完成 | OpenCC s2twp |
| segments.json 快取 | ✅ 完成 | 避免重跑 whisper |
| hash 資料夾命名 | ✅ 完成 | 解決中文路徑問題 |
| 音量峰值偵測 | ⚠️ 有限制 | 壓縮過的音訊動態範圍太小,效果差 |
| 完整長直播測試 | 🔄 測試中 | 75 分鐘直播,等確認輸出 |

### Phase 2：MVP（待做）

| 項目 | 狀態 | 備註 |
|------|------|------|
| 關鍵字打分 | ❌ 待做 | 需要月月提供常用梗詞 |
| 重複詞偵測 | ❌ 待做 | 短時間內同一詞出現 3+ 次 |
| 語速突變偵測 | ❌ 待做 | 每段字/秒 vs 全場平均 |
| 長靜音後爆發 | ❌ 待做 | segment gap > 3 秒 |
| channel.yaml 設定檔 | ❌ 待做 | 頻道專屬關鍵字 / 權重 |
| highlights.csv 表格輸出 | ❌ 待做 | 方便排序篩選 |

### Phase 3：進階功能（之後再說）

| 項目 | 狀態 | 備註 |
|------|------|------|
| pyannote 說話人分離 | ❌ 待做 | 多人聯動時用 |
| YouTube 彈幕密度分析 | ❌ 待做 | live chat replay JSON |
| --cut-clips ffmpeg 預剪 | ❌ 待做 | 自動切精華小段 mp4 |
| .srt 字幕輸出 | ❌ 待做 | 可匯入剪輯軟體 |
| Ollama 標題草稿 | ❌ 待做 | 本地 LLM 給每段下標題 |
| Premiere XML marker 輸出 | ❌ 待做 | 直接開進剪輯軟體 |

---

## 已知問題 & 解法

### 🔴 P0：GPU 模型釋放 segfault

**症狀**：whisper 辨識完成後 print 了「完成:N 段」，但之後的程式碼不執行，沒有 traceback，output 只有 audio.wav。

**原因**：faster-whisper 底層 CTranslate2 在釋放 GPU 記憶體時觸發 C 層級 segfault。Python 的 try/except 抓不到（不是 Python 例外，是作業系統砍掉程序）。

**解法**：在 `transcribe()` 函式 return 之前就存 `segments.json`，不等函式結束後才寫。第二次跑讀快取就不碰 GPU，不會觸發 bug。

**狀態**：✅ 已修（2026-05-06）

---

### 🔴 P0：Windows CUDA DLL 找不到

**症狀**：`RuntimeError: Library cublas64_12.dll is not found or cannot be loaded`

**原因**：pip 安裝的 `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` 把 DLL 放在 site-packages 底下，但 Windows 的 PATH 沒有包含那個目錄。

**解法**：每次開新 PowerShell 視窗手動加 PATH：
```powershell
$nv = "C:\Users\admin\AppData\Local\Programs\Python\Python313\Lib\site-packages\nvidia"
$env:PATH = "$nv\cublas\bin;$nv\cudnn\bin;" + $env:PATH
```

**狀態**：⚠️ workaround（每次要手動設。之後可寫 run.ps1 自動化）

---

### 🟡 P1：音量偵測對壓縮音訊無效

**症狀**：`highlights.md` 顯示 0 個峰值，保底 Top 20 的 dB 差距也很小。

**數據**：
```
音量分布: median=-19.4 | p90=-18.0 | p95=-17.8 | p99=-16.6 | max=-16.0 dB
動態範圍 (max - median) = 3.3 dB
```

**原因**：直播音訊經過 OBS / 麥克風的動態壓縮器（compressor），大聲小聲都被壓平，動態範圍只剩 3 dB（正常應該 10–20 dB）。

**解法方向**：
- 音量偵測保留但降低期望（只當輔助訊號）
- 主要改靠文字訊號：關鍵字、重複詞、語速突變
- 接 YouTube 彈幕密度（觀眾反應 = 最強訊號）

**狀態**：🔄 待 Phase 2 補其他訊號

---

### 🟡 P1：中文檔名路徑問題

**症狀**：output 資料夾用中文全名時，特殊符號（`？`、`｜`、空格）導致路徑不一致，寫檔失敗但無報錯。

**原因**：Windows + Python Path 對全形符號處理不一致。ffmpeg subprocess 建的資料夾跟 Python Path 認知的名稱可能有微妙差異。

**解法**：改用 `md5(檔名)[:8] + 前20安全字元` 當資料夾名。另存 `source.txt` 記錄原始檔名。

**狀態**：✅ 已修（2026-05-06）

---

### 🟢 P2：Whisper 中文輸出為簡體

**症狀**：逐字稿全是簡體中文（遇过、弹弹、没有）。

**原因**：Whisper 的中文模型不區分簡繁，預設輸出簡體。

**解法**：用 OpenCC（s2twp）後處理，簡體轉台灣正體（含詞彙轉換）。

**狀態**：✅ 已修（2026-05-05）

---

## 環境資訊

| 項目 | 值 |
|------|-----|
| OS | Windows（PowerShell 5.1）|
| Python | 3.13 |
| GPU | NVIDIA RTX 3060 Ti 8GB |
| faster-whisper | ≥1.0.0 |
| CUDA libs | cublas-cu12 12.9.2.10, cudnn-cu12 9.21.1.3 |
