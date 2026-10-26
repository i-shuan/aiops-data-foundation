#!/usr/bin/env python3
"""產生 deploy/cronjobs.yaml（第一階段 4 個）和 cronjobs-stage2.yaml（之後再開的 6 個）：每個指令一個 CronJob，同一個樣板。
改頻率、改參數改這裡的表，再跑 python3 deploy/gen-cronjobs.py。
第一階段只開 rules、detect、alert、train：能跑通「告警 → 回饋 → 調門檻」這一圈的最少集合，偵測來源少，誤報來了容易知道是哪裡出的。"""
import os

STAGE1 = ("rules", "detect", "alert", "train")
JOBS = [
    ("rules",         "* * * * *",    ["detector", "rules", "--config", "configs/baseline/rules.yaml"],   "狀態變化（容器重啟、錯誤率 5%、p95 2 秒保底…）要快", 50),
    ("detect",        "* * * * *",    ["detector", "detect", "--config", "configs/baseline/series.yaml", "--window", "10m"], "每分鐘看最近 10 分鐘；同一筆重跑會覆蓋", 50),
    # alert：一件新的 = 根因分析（幾秒）＋ LLM（真的模型最多 60 秒逾時）；一輪可能好幾件新的，給 10 分鐘。
    # Forbid 會擋住下一分鐘的，所以跑久一點只是晚一點，被砍才是什麼都沒寫（連心跳都沒有）
    ("alert",         "* * * * *",    ["investigator", "alert", "--config", "configs/rca/rca.yaml"],     "合成一件、根因分析、相似事故、LLM、心跳", 600),
    ("peer",          "*/5 * * * *",  ["detector", "peer", "--config", "configs/baseline/peer.yaml"],     "跟兄弟比看最近 10 分鐘", 240),
    ("trace-collect", "*/5 * * * *",  ["detector", "trace-collect", "--config", "configs/baseline/trace.yaml"], "trace 每一段的量", 240),
    ("trace-detect",  "*/5 * * * *",  ["detector", "trace-detect", "--config", "configs/baseline/trace.yaml", "--window", "10m"], "trace 每一段跟 baseline 比", 240),
    ("quality",       "*/10 * * * *", ["detector", "quality", "--config", "configs/baseline/quality.yaml"], "資料品質", 500),
    ("train",         "0 2 * * *",    ["detector", "train", "--config", "configs/baseline/series.yaml"],   "每天學過去 4 週", 3600),
    ("trace-train",   "20 2 * * *",   ["detector", "trace-train", "--config", "configs/baseline/trace.yaml"], "每天學 trace 每一段", 3600),
    ("chronic",       "0 8 * * 1",    ["detector", "chronic", "--config", "configs/baseline/chronic.yaml"], "每週一的慢性病清單", 1800),
]
HEAD = ["# 每個指令一個 CronJob（docs/pdca/2026-10-schedule.md 的表）。這個檔是 deploy/gen-cronjobs.py 產生的，改表要改那裡。",
        "# 第一階段只開這 4 個（rules、detect、alert、train）；其他 6 個在 cronjobs-stage2.yaml，四個跑穩了再在 kustomization 打開。",
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


HEAD2 = ["# 之後再開的 6 個（trace 每一段、跟兄弟比、資料品質、慢性病）：第一階段的 4 個在真實資料上跑穩、看過一輪誤報再一個一個打開",
         "# （kustomization.yaml 的 resources 把這個檔的註解拿掉）。這個檔也是 deploy/gen-cronjobs.py 產生的。"]


def render_all() -> dict[str, str]:
    s1 = [j for j in JOBS if j[0] in STAGE1]
    s2 = [j for j in JOBS if j[0] not in STAGE1]
    return {"cronjobs.yaml": "\n".join(HEAD + [render(*j) for j in s1]) + "\n",
            "cronjobs-stage2.yaml": "\n".join(HEAD2 + [render(*j) for j in s2]) + "\n"}


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    for name, text in render_all().items():
        with open(os.path.join(here, name), "w", encoding="utf-8") as f:
            f.write(text)
        print(f"{name}：{text.count('kind: CronJob')} 個 CronJob")
