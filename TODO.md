# TODO — StreamClip-Tool

## Phase 1 ✅ · Phase 2 ✅ · Phase 3 ✅

所有計畫功能已完成。以下為完整記錄。

## 已完成

### Phase 1：PoC 驗證（2026-05-06）
- [x] ffmpeg 抽音訊
- [x] faster-whisper ASR（large-v3）
- [x] OpenCC 簡轉繁
- [x] segments.json 快取
- [x] hash 資料夾命名
- [x] 音量峰值偵測
- [x] check.py 診斷工具

### Phase 2：MVP（2026-05-07 ~ 05-09）
- [x] **T1：SRT 字幕輸出**（2026-05-07）
- [x] **T2：channel.yaml loader**（2026-05-07）
- [x] **T3：長靜音後爆發偵測**（2026-05-07）
- [x] **T4：關鍵字打分**（2026-05-09）— 30 段命中
- [x] **T5：合併訊號成候選清單**（2026-05-07）— 六訊號加權合併
- [x] **T6：精華區段合併**（2026-05-07）
- [x] **T7：highlights.csv 輸出**（2026-05-07）
- [x] **選配：重複詞偵測**（2026-05-09）— 35 段命中
- [x] **選配：語速突變偵測**（2026-05-09）— 57 段觸發

### Phase 3：進階功能（2026-05-09）
- [x] **--cut-clips ffmpeg 預剪**（2026-05-09）— stream copy 秒切，30/30 成功
- [x] **YouTube 彈幕密度分析**（2026-05-09）— --chat-json，待提供聊天室 JSON 驗收
- [x] **Ollama 標題草稿**（2026-05-09）— --titles，待本機部署 Ollama 驗收
- [x] **剪輯標記輸出**（2026-05-09）— markers.edl + chapters.txt
