"""五個模型的入口：只吃標準輸入（DataFrame／list），不碰 ES、不讀設定檔（docs/pdca/meta/2026-11-model-modules.md）。

  models.baseline   M1 動態基準線學習：train（學平常）、score／score_poisson（打分）、mark_status（連續幾次才算）
  models.peer       M2 Pod 同儕橫向比對：compare
  models.rules      M3 靜態臨界值規則：evaluate（value／compare／ratio／increase／slope…）、evaluate_window
  models.trace      M4 trace 分段：summarize（每段平均）、new_segments、compare_segments（出事 vs 平常）
  models.similar    M5 相似事故檢索：fingerprint、rank、explain

演算法本體在 detector/、investigator/（沒搬，排程與既有測試照舊）；這裡是 import 進來用的門面，每個模組的 docstring 寫輸入格式，
check_*() 檢查欄位不對就說缺什麼。資料從哪來、怎麼整成這些格式：sources/。
"""
from . import baseline, peer, rules, similar, trace  # noqa: F401

__all__ = ["baseline", "peer", "rules", "similar", "trace"]
