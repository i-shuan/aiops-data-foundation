"""engine 的 API（沒有頁面的薄 API，畫面在 Grafana；docs/pdca/meta/2026-11-api-beat-worker.md）＋老師傅回饋的備用網頁
（docs/pdca/phase5-learning-loop/2026-10-expert-feedback.md；Grafana 的 Business Forms 外掛裝不起來時用）。

python3 -m main api  （環境變數 ES_URL、ES_API_KEY；API_PORT 預設 8080；API_CORS_ORIGIN 允許哪些網頁打 /api/*；AIOPS_API_TOKEN 寫入類端點的 token）
  POST /api/anomalies   {"input": "ml", "rows": [...]}：ML／FDC 把判好的異常推進 ⑤（跟 detector ingest 同一套對照，configs/<plugin>/ingest.yaml 的 inputs）；
                        要 Authorization: Bearer <AIOPS_API_TOKEN>；有佇列（AIOPS_BROKER_URL）就立刻丟一個 alert 工作、幾秒內開件
  POST /api/rca         {"alert_id": "..."}：在開著的那件上標 rca_requested，下一輪 alert 強制重跑根因分析（Grafana 的「重跑根因分析」按鈕打它）
  GET  /api/alerts?status=open&size=50   件的清單（給別的系統）
  GET  /feedback?alert=<alert_id>   備用表單：預填那件告警的摘要、根因分析、過去相似事故；老師傅只填判斷和經驗
  POST /feedback                    寫 aiops-feedback（一件的每筆異常各一筆，現有的 feedback summary／suggest 照樣算）
                                    和 aiops-incidents（確認 → status confirmed；根因錯 → corrected；誤報 → false_positive）
  GET  /report?alert=<alert_id>     根因報告全文
  GET  /health
HTTP 層是 FastAPI（routes.py），uvicorn 跑；邏輯在 learn/feedback_service.py，資料形狀在 dto.py，HTML 模板在 views/；路徑、狀態碼、回應格式跟原本 http.server 版一樣（Grafana 直接打這些 URL）。
身分先用表單裡填的名字（通知管道本身已經認得人）。
"""
