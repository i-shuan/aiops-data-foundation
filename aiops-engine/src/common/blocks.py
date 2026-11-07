"""功能積木（docs/pdca/meta/2026-11-blocks.md）：④⑥⑦⑨ 每一段一張登記表，情境資料檔的 `blocks:` 選要哪幾塊。

    blocks:                          # 情境資料檔（common/scenario/*.yaml）；沒寫的段 = 那一段的預設（多半是全部）
      detect: [dynamic_baseline, threshold_rule]          # ④ 只開這兩種判斷，其他指令跑了只印一行、回 0
      severity: [golden_signals]                          # ⑥ 不要 P0 升級
      similar: [fields, my_pkg.similar:by_ticket]         # ⑦ 欄位比對＋自己寫的檢索（套件.模組:名字）
      suggest: [false_positive, missed]                   # ⑨ 只提這兩種建議

登記表只記「名字 → 模組:名字」，第一次用才 import（跟 common/infra/repos.py 一樣），所以 common 不靜態依賴任何功能。
每一段的積木要長什麼樣（呼叫方式）寫在 STAGES 的 contract；自己寫的積木照那個寫法。看全部：`python3 -m main detector blocks`。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Stage:
    key: str                                   # 情境資料檔 blocks: 底下的鍵
    title: str                                 # 給人看的（模組編號＋名稱）
    builtin: dict[str, tuple[str, str]]        # 名字 → (模組:名字, 說明)；順序就是預設的順序
    contract: str                              # 自己寫的積木要長什麼樣
    one: bool = False                          # True：只能選一塊（併件）
    default: tuple[str, ...] | None = None     # 情境沒寫時用哪些；None = 全部內建
    fallback_attr: str | None = None           # 情境沒寫 blocks 時改看 Scenario 的這個屬性（報告段落沿用 sections）
    extra: dict[str, str] = field(default_factory=dict)   # 內建名字 → 對應的指令（④ 的積木是指令；給 detector blocks 列出來）

    def defaults(self) -> list[str]:
        return list(self.default if self.default is not None else self.builtin)

    def enabled(self, scenario) -> list[str]:
        """這個情境開了哪幾塊（照順序）。寫錯名字丟 ValueError，講清楚有哪些。"""
        got = (getattr(scenario, "blocks", None) or {}).get(self.key)
        if got is None and self.fallback_attr:
            got = getattr(scenario, self.fallback_attr, None)
        names = self.defaults() if got is None else [str(x) for x in got]
        bad = [n for n in names if n not in self.builtin and ":" not in n]
        if bad:
            raise ValueError(f"blocks.{self.key} 不認得 {'、'.join(bad)}：內建的有 {'、'.join(self.builtin)}；"
                             f"自己寫的用「套件.模組:名字」")
        if self.one and len(names) != 1:
            raise ValueError(f"blocks.{self.key} 只能選一塊（現在是 {names}）")
        return names

    def on(self, scenario, name: str) -> bool:
        return name in self.enabled(scenario)

    def load(self, name: str):
        ref = self.builtin[name][0] if name in self.builtin else name
        mod, _, attr = ref.partition(":")
        try:
            return getattr(importlib.import_module(mod), attr)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"blocks.{self.key} 的 {name}（{ref}）載入不了：{e}") from e

    def loaded(self, scenario) -> list[tuple[str, object]]:
        """開著的積木：[(名字, 函式或類別)]，照順序。"""
        return [(n, self.load(n)) for n in self.enabled(scenario)]


STAGES: dict[str, Stage] = {s.key: s for s in (
    Stage("detect", "④ 判斷", {
        "dynamic_baseline": ("detect.detect_service:cmd_detect", "動態基準線學習：每條序列 × 星期幾 × 小時的平常值，偏太多就報"),
        "peer_compare": ("detect.strategy.peer_compare:cmd_peer", "Pod 同儕橫向比對：同服務的 Pod 只有一隻不對勁"),
        "threshold_rule": ("detect.strategy.threshold_rule:cmd_rules", "靜態臨界值規則：一定不能超過的線"),
        "chronic_issue": ("detect.strategy.chronic_issue:cmd_chronic", "慢性病清單：天天噴、沒人修的（每週）"),
        "quality_check": ("detect.series_ops.quality_check:cmd_quality", "資料品質檢查：路由沒參數化、欄位缺、資料停了"),
        "level_shift": ("detect.series_ops.level_shift:cmd_shift", "一階跳變：停在新水準（含只判往上的往下掉）→ 提重新學"),
        "trace_latency": ("detect.trace.latency_stats:cmd_trace_detect", "trace 每一段的統計與偵測"),
    }, "內建的是指令（關掉的指令跑了只印一行、回 0）；自己寫的是 register(app)：在 detector 加自己的子命令，跟情境的 commands 一樣",
        extra={"dynamic_baseline": "train、detect、tune", "peer_compare": "peer", "threshold_rule": "rules", "chronic_issue": "chronic",
               "quality_check": "quality", "level_shift": "shift", "trace_latency": "trace-collect、trace-train、trace-detect"}),
    Stage("grouping", "⑥ 收斂：併件", {
        "same_entity": ("incident.alert_grouper:merge", "同一個對象（服務）、時間接著來的合成一件；跨服務、前後站照情境的 chain"),
    }, "merge(open_alerts, anomalies, now, close_after, done_ids, calls, links, scenario) -> list[Alert]（跟 incident/alert_grouper.merge 一樣）",
        one=True),
    Stage("severity", "⑥ 收斂：嚴重度", {
        "golden_signals": ("incident.alert_severity:golden_signals", "幾類黃金訊號、持續多久、換版後上限 warning"),
        "p0": ("incident.alert_severity:p0", "P0 服務（alerts.p0_services）升一級"),
    }, "fn(ctx: SeverityContext, g: dict | None) -> dict：一串依序跑，前一塊的結果是下一塊的 g（第一塊收到 None）；"
       "g 要有 severity、why"),
    Stage("rca_sections", "⑦ 根因分析：報告段落", {
        name: (f"incident.rca_report:sec_{name}", desc) for name, desc in (
            ("anomalies", "這段時間的異常"), ("entries", "這個服務收到的請求跟平常比"), ("segments", "慢在哪一段、錯從哪開始、共同點"),
            ("slowest", "最慢的請求的瀑布圖"), ("logs", "新的 log 樣式"), ("changes", "出事前的變更"), ("topology", "呼叫關係"),
            ("conclusion", "結論"))
    }, "fn(c: Ctx) -> None：往 c.out 加 Markdown 的行、往 c.findings 放結構化的結果（跟 incident/rca_report 的 sec_* 一樣）",
        fallback_attr="sections"),
    Stage("similar", "⑦ 根因分析：相似事故", {
        "fields": ("llm.similar:field_candidates", "欄位比對：同一段變慢、同一個錯誤源頭、同服務…（看得出為什麼像）"),
        "vector": ("llm.similar:vector_candidates", "向量輔助：說法不同但意思一樣（要設 EMBEDDING_URL、EMBEDDING_MODEL）"),
    }, "fn(repo, fp, text, exclude_id, opts) -> list[(事故 id, 文件, 向量相似度或 None)]：候選，分數照舊由欄位比對＋相似度算"),
    Stage("summary", "⑦ 根因分析：摘要", {
        "llm": ("incident.alert_service:llm_summary", "LLM 摘要（只加註、每句附依據；provider 由 LLM_PROVIDER 決定）"),
    }, "fn(al, findings, similar, alerts_config, unit) -> {ok, text, provider…} 或 None（跟 incident/alert_service.llm_summary 一樣）"),
    Stage("hints", "⑦ 根因分析：歷史 case", {
        "segment_hints": ("llm.similar:hint_lines", "線索：這次已經看到變化、過去也出事過的段，件數多的排前面"),
        "checklist": ("llm.similar:checklist_lines", "先查什麼：同一類事故老師傅勾過的排前面，不夠用情境的 checklists 補"),
    }, "fn(findings, cases, cls, scenario) -> list[str]：報告最前面多的幾行（Markdown）；cases 是確認過的事故 [(id, 文件)]"),
    Stage("suggest", "⑨ 回饋：調整建議", {
        "false_positive": ("learn.feedback_service:suggest_false_positives", "誤報：放寬門檻、時段放寬、至少差多少才報"),
        "missed": ("learn.feedback_service:suggest_missed", "漏報：門檻降成 0.8 倍"),
        "precision": ("learn.feedback_service:suggest_precision", "整條序列的準確率：太低放寬、太高收緊"),
        "rule_upgrade": ("learn.feedback_service:suggest_rules", "同根因確認 N 次：升級成靜態臨界值規則"),
        "from_text": ("learn.feedback_service:propose_from_text", "回饋的一句話：LLM 抽成調整（送出回饋時）"),
    }, "fn(ctx: SuggestContext) -> int：提了幾筆（寫進 aiops_tuning，status proposed）；from_text 是送出回饋時跑的，不在 feedback suggest 裡"),
)}


def stage(key: str) -> Stage:
    return STAGES[key]


def enabled(key: str, scenario) -> list[str]:
    return STAGES[key].enabled(scenario)


def on(key: str, scenario, name: str) -> bool:
    return STAGES[key].on(scenario, name)


def describe(scenario) -> list[str]:
    """`detector blocks` 印的：每一段有哪些積木、這個情境開了哪幾塊。"""
    lines = []
    for s in STAGES.values():
        try:
            on_ = s.enabled(scenario)
            err = ""
        except ValueError as e:
            on_, err = [], str(e)
        lines.append(f"{s.title}（blocks.{s.key}{'，只能選一塊' if s.one else ''}）")
        for name, (ref, desc) in s.builtin.items():
            cmd = f"　指令：{s.extra[name]}" if name in s.extra else ""
            lines.append(f"  {'✓' if name in on_ else '·'} {name:18} {desc}{cmd}")
        for name in on_:
            if name not in s.builtin:
                lines.append(f"  ✓ {name:18} （自己寫的）")
        if err:
            lines.append(f"  ⚠ {err}")
        lines.append(f"    自己寫的積木：{s.contract}")
    return lines
