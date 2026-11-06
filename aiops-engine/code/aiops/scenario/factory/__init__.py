"""工廠（半導體）：對象是機台、「API」是 recipe、異常由 ML／FDC 判好接進 ⑤（detector ingest）、履歷（lot-paths-*）取代 trace、
後站的異常依 lot 併進前站、recipe 變更當換版、嚴重度依參數家族。docs/pdca/meta/2026-11-factory-engine.md、configs/factory/README.md。
"""
from __future__ import annotations

from datetime import datetime

from aiops.scenario.base import Chain, Plugin as Base
from aiops.service.rca.report import _dur

from aiops.scenario.factory.chain import lot_links
from aiops.scenario.factory.change import change_at
from aiops.scenario.factory.commands import register

PROFILE = {
    # 機台歸屬（廠區、設備工程師群）在機台主檔 eqp-master：一台一筆、沒有時間欄位（static），engine 只讀
    "owner": {"index": "eqp-master", "namespace": "area", "service": "equipment_id", "dept": "dept", "group": "group", "static": True},
    # 「log」是機台 alarm（S5F1）、PM／維修紀錄、recipe 變更，合在 eqp-logs-*：一列一筆，equipment_id、level、lot_id（有的話）、
    # message；event_type（alarm、pm、recipe_change）是哪一種，level 不是 warn／error 的 PM、recipe 變更也要撈
    "log": {"service": "equipment_id", "level": "level", "trace_id": "lot_id", "message": ["message", "alarm_text"],
            "problem_levels": ["warn", "warning", "error", "alarm", "critical", "WARN", "WARNING", "ERROR", "ALARM", "CRITICAL"],
            "problem_query": {"terms": {"event_type": ["alarm", "pm", "recipe_change"]}}},
}

DOMAIN = {
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
}

# 序列名 → 參數家族；幾個家族同時異常＝嚴重度的廣度（IT 是延遲／錯誤／往外呼叫／飽和）。cfg 的 alerts.severity.classes 可覆蓋
SEVERITY_CLASSES = {
    "pressure": ["pressure", "vacuum"], "temperature": ["temp"], "flow": ["flow", "gas"],
    "power": ["power", "rf", "bias", "current", "voltage"], "time": ["time", "endpoint", "rate"],
    "metrology": ["cd", "overlay", "thickness", "defect", "particle", "yield"], "alarm": ["alarm", "oos"],
}


class Plugin(Base):
    name = "factory"
    entity_keys = ("equipment",)                          # detector/ingest.py 寫的 keys.equipment；一台機台一件
    critical_levels = ("rule", "peer", "quality", "fdc")  # FDC 的 OOS 鳴叫（severity critical）直接讓這件 critical
    severity_classes = SEVERITY_CLASSES
    schedule = "configs/factory/schedule.yaml"

    def source_profile(self) -> dict:
        return PROFILE

    def domain(self) -> dict:
        return DOMAIN

    def route_of(self, keys: dict) -> str | None:
        """「哪支 API」是哪個 recipe（履歷裡站的 name 就是 recipe，根因分析用它挑 lot）。"""
        return str(keys["recipe"]) if keys.get("recipe") else None

    def chain(self, repo, cfg, anomalies, open_alerts, start, now) -> Chain:
        """前後站：這一輪帶 lot 的異常 → 從履歷找這批 lot 之前經過哪幾站（plugins/factory/chain.py）。"""

        acfg = cfg.get("alerts", {})
        return Chain(links=lot_links(repo, cfg["indices"]["traces"], anomalies, now, _dur(str(acfg.get("paths_lookback", "3d")))))

    def change_at(self, repo, cfg, entity, since, now) -> datetime | None:
        """recipe 變更紀錄當換版（plugins/factory/changes.py）。"""
        acfg = cfg.get("alerts", {})
        return change_at(repo, acfg.get("deploy_index", "eqp-logs-*"), acfg.get("deploy_entity_field", "equipment_id"), entity,
                         since, now, acfg.get("deploy_time_field", "@timestamp"), acfg.get("deploy_filter", {"term": {"event_type": "recipe_change"}}))

    def commands(self) -> dict:
        return {"paths-merge": register}
