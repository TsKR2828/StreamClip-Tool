# TODO — Phase 2 MVP

## 依賴圖

```
T1 ✅ SRT 輸出
T2 ✅ channel.yaml loader
T3 ✅ 長靜音後爆發偵測
T4 ✅ 關鍵字打分 ← T2 + 梗詞清單
T5 ✅ 合併訊號 ← T3 + T4 + 音量 peaks
T6 ✅ 區段合併 ← T5
T7 ✅ highlights.csv ← T6
```

## Phase 2 核心：全部完成

## 選配（看命中率再決定）

- [ ] 重複詞偵測：同詞 3+ 次短時間出現 → 加分
- [ ] 語速突變偵測：字/秒偏離全場均值 → 加分

## 已完成

- [x] **T1：SRT 字幕輸出**（2026-05-07）
- [x] **T2：channel.yaml loader**（2026-05-07）
- [x] **T3：長靜音後爆發偵測**（2026-05-07）
- [x] **T4：關鍵字打分**（2026-05-09）— score_keywords() 全文比對 + 多次命中加乘，30 段命中
- [x] **T5：合併訊號成候選清單**（2026-05-07）— volume + silence + keyword 三訊號加權合併
- [x] **T6：精華區段合併**（2026-05-07）— merge_gap + padding + top_n 篩選 + 逐字稿附加
- [x] **T7：highlights.csv 輸出**（2026-05-07）— pandas CSV，utf-8-sig for Excel
