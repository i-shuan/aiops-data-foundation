"""領域 profile：IT（對象是服務）或工廠（對象是機台）。環境變數 AIOPS_DOMAIN 選：it（預設）、factory。

決定 YAML 碰不到、但跟「這是什麼地方」有關的幾張表：回饋表單的根因分類、誤報原因、常查的地方，以及對象怎麼稱呼。
程式其他地方的「service」欄位在工廠版就是機台（investigator/alerts.py 的 SVC_KEYS 認 equipment）；索引、欄位名在 core/source.py。
docs/pdca/meta/2026-11-factory-engine.md。
"""
from __future__ import annotations

import os

DOMAINS = {
    "it": {
        "entity": "服務", "unit": "請求", "trace": "Trace", "new_value_hint": "換版後的新 Pod、新版本",
        "categories": [("db", "DB（慢查詢、鎖、連線池、統計資訊）"), ("network", "網路（延遲、斷線、DNS）"), ("config_change", "設定變更"),
                       ("deploy", "版本更新"), ("external", "外部系統（供應商、其他部門的服務）"), ("resource", "資源不足（CPU、記憶體、磁碟）"),
                       ("bug", "程式錯誤"), ("data", "資料問題（髒資料、量暴增）"), ("other", "其他")],
        "fp_reasons": [("batch", "月底 / 定時批次本來就這樣"), ("loadtest", "壓測、演練"), ("known_slow", "這支 API 本來就慢"),
                       ("low_volume", "量太少，幾筆就跳"), ("deploy", "換版期間"), ("other", "其他")],
        "checked": ["Oracle AWR / 慢查詢", "DB 連線池", "主機 CPU / 記憶體 / 磁碟", "網路 / DNS", "Kibana log", "trace 瀑布圖",
                    "Kafka lag", "近期的版本更新 / 設定變更", "上游 / 下游服務", "外部系統狀態"],
    },
    "factory": {
        "entity": "機台", "unit": "lot", "trace": "履歷", "new_value_hint": "新版 recipe、新批材料、剛 PM 完的 chamber",
        # 半導體的根因分類（docs/semiconductor-design.md「資料與模型的差別」）
        "categories": [("equipment", "機台硬體（真空、溫控、手臂、零件）"), ("process_drift", "製程漂移（參數慢慢偏）"),
                       ("recipe", "recipe（版本、參數設定）"), ("material", "材料（氣體、化學品、靶材、批號）"),
                       ("metrology", "量測誤差（量測機、校正）"), ("pm_related", "PM／保養之後（剛保養、換零件）"),
                       ("operator", "人為操作"), ("other", "其他")],
        # 工廠的誤報調不到 ML 的門檻（engine 不判），原因只用來匯出給 ML 團隊重訓（detector feedback export）
        "fp_reasons": [("pm_just_done", "剛做完 PM，暖機中"), ("qual_run", "qual／dummy／測試 run"), ("engineering_lot", "工程 lot，本來就不一樣"),
                       ("known_drift", "已知的漂移，有人在追"), ("sensor_noise", "感測器雜訊、量測誤差"), ("recipe_change", "recipe 剛換"),
                       ("other", "其他")],
        "checked": ["FDC 趨勢圖", "機台 alarm", "PM／維修紀錄", "recipe 版本與參數", "chamber 互比", "量測結果（CD、厚度、良率）",
                    "材料批號", "前站機台", "EAP／通訊 log"],
    },
}


def name() -> str:
    d = os.environ.get("AIOPS_DOMAIN", "it")
    if d not in DOMAINS:
        raise ValueError(f"AIOPS_DOMAIN 只能是 {'、'.join(DOMAINS)}，不是 {d!r}")
    return d


def profile(section: str):
    return DOMAINS[name()][section]


def categories() -> list[tuple[str, str]]:
    return list(profile("categories"))


def category_codes() -> tuple[str, ...]:
    return tuple(c for c, _ in categories())


def fp_reasons() -> list[tuple[str, str]]:
    return list(profile("fp_reasons"))


def checked() -> list[str]:
    return list(profile("checked"))


def entity_label() -> str:
    """對象叫什麼：服務／機台。"""
    return profile("entity")


def unit_label() -> str:
    """被追蹤的一個單位叫什麼：請求／lot（報告、摘要的措辭）。"""
    return profile("unit")


def unit_text() -> str:
    """unit_label 放進中文句子用：英文的（lot）前後補空格，「每個 lot 多花」。"""
    u = unit_label()
    return f" {u} " if u.isascii() else u


def trace_label() -> str:
    """把一個單位串起來的那份資料叫什麼：Trace／履歷。"""
    return profile("trace")


def new_value_hint() -> str:
    """共同點是「平常沒見過的值」時，提示可能是什麼。"""
    return profile("new_value_hint")
