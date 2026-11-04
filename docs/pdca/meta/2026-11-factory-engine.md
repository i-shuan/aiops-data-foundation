# 工廠版 engine：復用程式、另一套設定，ML 的異常接進 ⑤、生產履歷取代 trace（2026-11）

**狀態**：進行中　　**日期**：2026-11-04　　**相關**：分支 `claude/factory-engine`（接在 `claude/semiconductor-design` 後）；設計 [semiconductor-design.md](../../semiconductor-design.md)
**模組**：④⑤⑥⑦⑨

## Plan：這一次要達到什麼

- **需求**：工廠（半導體 FDC）要用 AIOps，但沒有 trace id；異常由 ML 團隊的模型判斷（異常跳點）加 FDC 鳴叫系統，engine 不必再判。廠方目前沒有明確需求，先跑出一版給他們看。
- **目的**：同一份 engine 程式、另一套設定（`configs/factory/`）能從 ⑤ 開始跑完整條：ML／FDC 的異常 → 收斂成一件 → 用生產履歷定位到站點、機台、chamber、recipe → 通知 → 老師傅回饋。用 `source: file` 餵假資料就能整條走一遍，不等廠方資料。
- **範圍**：
  - 做：(1) ④ 的「接收器」：把 ML 輸出、FDC 鳴叫轉成 `aiops-anomalies` 格式（欄位：`keys.equipment`、`keys.chamber`、`keys.recipe`、`keys.step`、`series` 是參數名）；(2) ⑥ 收斂：併件的鍵從 service 換成 equipment／chamber，跨站依同一批 lot 的前後站併一層；(3) ⑦ 履歷合成器：MES 進出站為主幹、S6F11 機台事件為子段，轉成現有 `core/traces` 的 span 格式，M4 不改；(4) ⑦ log 的來源換成機台 alarm（S5F1）、PM／維修、recipe 變更（recipe 變更套用現有「換版後算 warning」）；(5) ⑨ 誤報標記匯出給 ML 團隊（engine 調不到 ML 的門檻），case 分類表換成半導體的；(6) 假資料產生器＋`e2e` 腳本。
  - 不做：engine 自己對 FDC 感測值跑 M1～M3（ML 已判）；chamber matching（M2）；RCA graph；真實 EAP／MES 介接（欄位名全是假設，拿到樣本再對）；LLM 摘要（廠內能否用 LLM未知，先走 `template`）。
- **決定**：復用不 fork。理由：engine 有價值的是 ⑥～⑨，跟資料從哪來無關；`sources/` 已抽好介面；兩套設定分開部署，索引前綴、分類表、通知對象各自一份。
- **假設（未驗證，等廠方確認）**：ML 輸出一個 run 一筆，帶 lot、機台、chamber、參數、分數；FDC 鳴叫紀錄帶 lot id；EAP 有把 S6F11 存進 DB；撈得到 MES 進出站；EPT（E116）不帶 lot，只當機台狀態的背景資訊。

## Do：改動哪些 code、改了什麼、為何要改

無（尚未開工）。

## Check：測試的目的與結果

無。

## Act：Check 後沒改善的、還沒解決的

無。

- **下一輪**：先做 (1) 接收器＋(6) 假資料，整條用假資料跑通，再做 (3) 履歷合成器。
