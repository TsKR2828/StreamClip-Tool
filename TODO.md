# TODO — Phase 2 MVP

## 依賴圖

```
T1 ✅ SRT 輸出
T2 ✅ channel.yaml loader
T3 ✅ 長靜音後爆發偵測

T4 關鍵字打分 ← 需要 T2 + 梗詞清單
T5 ✅ 合併訊號 ← T3 + (T4) + 音量 peaks
T6 區段合併 ← T5
T7 highlights.csv ← T6
```

## 待辦

- [ ] **T4：關鍵字打分**
  - 讀取 channel.yaml 的 keywords dict
  - 對每個 segment.text 做全文比對，命中 → 加對應權重分
  - 輸出：segments + keyword_score 欄位
  - **前置：月月提供 channel.yaml 梗詞清單**

- [ ] **T6：精華區段合併（Step 5）**
  - 相鄰 ≤ merge_gap_sec 的候選合併
  - 前後各擴 padding_sec
  - 多訊號命中 → reasons 合併

- [ ] **T7：highlights.csv 輸出**
  - pandas DataFrame
  - 欄位：rank, start, end, duration, score, reasons, transcript_excerpt
  - 按 score 降序排列

## 選配（看 T4-T7 跑完結果再決定）

- [ ] 重複詞偵測：同詞 3+ 次短時間出現 → 加分
- [ ] 語速突變偵測：字/秒偏離全場均值 → 加分

## 已完成

- [x] **T1：SRT 字幕輸出**（2026-05-07）
- [x] **T2：channel.yaml loader**（2026-05-07）
- [x] **T3：長靜音後爆發偵測**（2026-05-07）
- [x] **T5：合併訊號成候選清單**（2026-05-07）— volume + silence 加權，T4 keyword 預留接口
