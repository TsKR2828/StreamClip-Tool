# DEV-LOG：開發進度 & 問題回報

## 開發進度

### Phase 1：PoC 驗證（✅ 完成）

| 項目 | 狀態 | 備註 |
|------|------|------|
| ffmpeg 抽音訊 | ✅ 完成 | 16kHz mono wav |
| faster-whisper 語音辨識 | ✅ 完成 | large-v3, RTF≈0.14 |
| 簡體 → 繁體轉換 | ✅ 完成 | OpenCC s2twp |
| segments.json 快取 | ✅ 完成 | 避免重跑 whisper + 繞過 GPU segfault |
| hash 資料夾命名 | ✅ 完成 | 解決中文路徑問題 |
| 音量峰值偵測 | ✅ 完成 | 動態範圍足夠時有效（16 dB）;壓縮音訊需靠其他訊號 |
| 完整長直播測試 | ✅ 通過 | 見下方測試結果 |
| check.py 診斷工具 | ✅ 完成 | 涵蓋率、空白偵測 |

#### 長直播測試結果（2026-05-06）

- 素材：75.7 分鐘雜談直播
- ASR：1713 段,RTF=0.14（耗時 645s）
- 涵蓋：3.3 分 → 75.6 分,語音涵蓋率 82.4%
- 最大空白：13.9 秒（正常,看彈幕/喝水）
- 音量：動態範圍 16.0 dB,344 個峰值（--peak-db 4）,Top 10 可用
- 繁體轉換：正常
- 結論：PoC 驗證通過,可推進 MVP

### Phase 2：MVP（🔄 進行中）

| ID | 項目 | 狀態 | 備註 |
|----|------|------|------|
| T1 | SRT 字幕輸出 | ✅ 完成 | 整數毫秒運算、無 BOM |
| T2 | channel.yaml loader | ✅ 完成 | 深度合併預設值 + --channel CLI arg |
| T3 | 長靜音後爆發偵測 | ✅ 完成 | 75 分鐘實測 24→18 筆（過濾雜訊） |
| T4 | 關鍵字打分 | ❌ 待做 | 需要月月提供常用梗詞 |
| T5 | 合併訊號成候選清單 | ✅ 完成 | volume + silence 加權合併，T4 keyword 預留接口 |
| T6 | 精華區段合併（Step 5） | ✅ 完成 | 合併相鄰 ≤ merge_gap_sec + padding + top_n 篩選 |
| T7 | highlights.csv 表格輸出 | ✅ 完成 | pandas CSV（utf-8-sig for Excel）+ 逐字稿截斷 200 字 |

#### Phase 2 選配（看命中率再決定）

| 項目 | 狀態 | 備註 |
|------|------|------|
| 重複詞偵測 | ❌ 待做 | 同一詞 3+ 次短時間出現 |
| 語速突變偵測 | ❌ 待做 | 每段字/秒 vs 全場平均 |

### Phase 3：進階功能（之後再說）

| 項目 | 狀態 | 備註 |
|------|------|------|
| --cut-clips ffmpeg 預剪 | ❌ 待做 | 自動切精華小段 mp4 |
| YouTube 彈幕密度分析 | ❌ 待做 | live chat replay JSON |
| Ollama 標題草稿 | ❌ 待做 | 本地 LLM 給每段下標題 |
| Premiere XML marker 輸出 | ❌ 待做 | 直接開進剪輯軟體 |

### 永遠不做

| 項目 | 理由 |
|------|------|
| pyannote 說話人分離 | 單人直播不需要 |
| YAMNet 笑聲偵測 | 文字訊號「www / 草」夠用 |
| GUI | CLI 足矣 |

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

**狀態**：🔄 待 Phase 2 T4-T5 補其他訊號

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

### 🟢 P2：SRT 檔案 BOM 導致播放器無法解析

**症狀**：產出的 `transcript.srt` 用媒體播放器/剪輯軟體打不開。

**原因**：`write_text(encoding="utf-8-sig")` 會在檔頭加 BOM（`\xef\xbb\xbf`），大多數 SRT 解析器不接受。

**解法**：改用 `encoding="utf-8"`（無 BOM）+ 整數毫秒運算避免 ms=1000 溢位。

**狀態**：✅ 已修（2026-05-07）

---

## 環境資訊

| 項目 | 值 |
|------|-----|
| OS | Windows 10 Home（PowerShell 5.1）|
| Python | 3.13 |
| GPU | NVIDIA RTX 3060 Ti 8GB |
| faster-whisper | ≥1.0.0 |
| CUDA libs | cublas-cu12 12.9.2.10, cudnn-cu12 9.21.1.3 |
