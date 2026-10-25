#!/usr/bin/env python3
"""產生 deploy/cronjobs.yaml：每個指令一個 CronJob，同一個樣板。改頻率、改參數改這裡的表，再跑 python3 deploy/gen-cronjobs.py。"""
import os

JOBS = [
    ("rules",         "* * * * *",    ["detector", "rules", "--config", "configs/baseline/rules.yaml"],   "狀態變化（容器重啟、錯誤率 5%、p95 2 秒保底…）要快", 50),
    ("detect",        "* * * * *",    ["detector", "detect", "--config", "configs/baseline/series.yaml", "--window", "10m"], "每分鐘看最近 10 分鐘；同一筆重跑會覆蓋", 50),
    ("alert",         "* * * * *",    ["investigator", "alert", "--config", "configs/rca/rca.yaml"],     "合成一件、根因分析、相似事故、心跳", 110),
    ("peer",          "*/5 * * * *",  ["detector", "peer", "--config", "configs/baseline/peer.yaml"],     "跟兄弟比看最近 10 分鐘", 240),
    ("trace-collect", "*/5 * * * *",  ["detector", "trace-collect", "--config", "configs/baseline/trace.yaml"], "trace 每一段的量", 240),
    ("trace-detect",  "*/5 * * * *",  ["detector", "trace-detect", "--config", "configs/baseline/trace.yaml", "--window", "10m"], "trace 每一段跟 baseline 比", 240),
    ("quality",       "*/10 * * * *", ["detector", "quality", "--config", "configs/baseline/quality.yaml"], "資料品質", 500),
    ("train",         "0 2 * * *",    ["detector", "train", "--config", "configs/baseline/series.yaml"],   "每天學過去 4 週", 3600),
    ("trace-train",   "20 2 * * *",   ["detector", "trace-train", "--config", "configs/baseline/trace.yaml"], "每天學 trace 每一段", 3600),
    ("chronic",       "0 8 * * 1",    ["detector", "chronic", "--config", "configs/baseline/chronic.yaml"], "每週一的慢性病清單", 1800),
]
HEAD = ["# 每個指令一個 CronJob（docs/pdca/2026-10-schedule.md 的表）。這個檔是 deploy/gen-cronjobs.py 產生的，改表要改那裡。",
        "# 共同點：concurrencyPolicy Forbid（上一次沒跑完就跳過這次）、startingDeadlineSeconds（錯過就不補跑，下一分鐘再來）、",
        "#         activeDeadlineSeconds（卡住就砍）、只留最近 1 次成功、3 次失敗的 Pod 方便看 log。時區跟著 TZ（CronJob 的 timeZone）。"]


def render(name, cron, args, why, deadline):
    return f"""---
# {why}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: aiops-{name}
  labels: {{app.kubernetes.io/name: aiops-engine, app.kubernetes.io/component: {name}}}
spec:
  schedule: "{cron}"
  timeZone: Asia/Taipei
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: {min(deadline, 300)}
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 0
      activeDeadlineSeconds: {deadline}
      template:
        metadata:
          labels: {{app.kubernetes.io/name: aiops-engine, app.kubernetes.io/component: {name}}}
        spec:
          restartPolicy: Never
          containers:
            - name: engine
              image: aiops-engine
              args: {args}
              envFrom:
                - configMapRef: {{name: aiops-env}}
                - secretRef: {{name: aiops-secrets}}
              resources:
                requests: {{cpu: 100m, memory: 512Mi}}
                limits: {{memory: {"4Gi" if name in ("train", "trace-train") else "1Gi"}}}
              # 設定檔要蓋掉 image 裡的：打開 kustomization 的 aiops-configs，再把下面打開
              # volumeMounts: [{{name: configs, mountPath: /app/configs/baseline/series.yaml, subPath: series.yaml}}]
          # volumes: [{{name: configs, configMap: {{name: aiops-configs}}}}]"""


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cronjobs.yaml")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(HEAD + [render(*j) for j in JOBS]) + "\n")
    print(f"{out}：{len(JOBS)} 個 CronJob")
