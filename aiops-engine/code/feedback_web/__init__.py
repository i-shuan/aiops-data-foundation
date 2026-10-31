"""老師傅的回饋入口（docs/pdca/phase5-learning-loop/2026-10-expert-feedback.md）：一個小網頁，通知裡的連結點進來，1 分鐘內填完。

python3 -m feedback_web  （環境變數 ES_URL、ES_API_KEY；FEEDBACK_PORT 預設 8080）
  GET  /feedback?alert=<alert_id>   表單：預填那件告警的摘要、根因分析、過去相似事故；老師傅只填判斷和經驗
  POST /feedback                    寫 aiops-feedback（一件的每筆異常各一筆，現有的 feedback summary／suggest 照樣算）
                                    和 aiops-incidents（確認 → status confirmed；根因錯 → corrected；誤報 → false_positive）
  GET  /report?alert=<alert_id>     根因報告全文
  GET  /health
只用標準函式庫（http.server），不另外裝套件。身分先用表單裡填的名字（通知管道本身已經認得人）。
"""
