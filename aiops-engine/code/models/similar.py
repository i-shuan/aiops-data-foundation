"""M5 相似事故檢索：一件新事故 → 指紋 → 在老師傅確認過的事故裡找最像的幾件，附為什麼像。原理 investigator/similar.py 檔頭。

輸入：
  alert      新事故（aiops-alerts 的文件：service、routes、series…）
  findings   根因分析的結果（segments、error_origins、new_patterns…），沒有就 None
  candidates [(id, 事故文件), …]：事故文件要有 fingerprint（fingerprint() 的格式）、status；只有 status 是 confirmed／corrected 的才拿來教人

用法（case 只做分類，不給根因、處置）：
  fp = fingerprint(alert, findings)
  top = rank(fp, confirmed_only(candidates, exclude_id=alert["alert_id"]), k=3)   # [{incident_id, score, why, root_cause_category…}]
  cls = classify(top)                                                              # {"category", "confidence", "votes", "n", "based_on"} 或 None
  evaluate_cases(cases)                                                            # 留一法：recall@3、accuracy
  learn_weights(cases)                                                             # case ≥ 50 件時學特徵權重
"""
from __future__ import annotations

from investigator.similar import (CONFIRMED, MIN_SCORE, TOP_K, WEIGHTS, classify, confirmed_only, describe_class,  # noqa: F401
                                  evaluate_cases, explain, fingerprint, format_similar, learn_weights, rank)


def check_candidates(candidates) -> list[tuple[str, dict]]:
    out = []
    for i, c in enumerate(candidates):
        if not (isinstance(c, (tuple, list)) and len(c) == 2 and isinstance(c[1], dict)):
            raise ValueError(f"第 {i} 個候選要是 (id, 文件)，不是 {type(c).__name__}")
        if "fingerprint" not in c[1]:
            raise ValueError(f"候選 {c[0]} 沒有 fingerprint（用 fingerprint(alert, findings) 算）")
        out.append((c[0], c[1]))
    return out
