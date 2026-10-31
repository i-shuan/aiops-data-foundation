"""engine 定期工作的排程器：一隻常駐的行程照 configs/schedule/schedule.yaml 跑 detector、investigator 的指令（子行程）。
取代每個指令一個 K8s CronJob（docs/pdca/phase3-locate/2026-10-single-pod-scheduler.md）。只用標準函式庫。"""
