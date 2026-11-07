"""管線的各段（④⑥⑦⑨）與外掛的選用（docs/pdca/meta/2026-11-superset-style.md）。

外掛本身在 plugins/plugin_*/（對應 superset-frontend/plugins/），入口啟動時 MainPreset().register() 登記進 core 的三張登記表；
這裡只定義「有哪幾段、每段的外掛長什麼樣（contract）」，以及情境資料檔的 `blocks:` 選哪幾個：

    blocks:                          # 情境資料檔（common/scenario/*.yaml）；沒寫的段 = 那一段登記了的全部（照登記順序）
      detect: [dynamic_baseline, threshold_rule]          # ④ 只開這兩種判斷，其他指令跑了只印一行、回 0
      severity: [golden_signals]                          # ⑥ 不要 P0 升級
      similar: [fields, my_pkg.similar:by_ticket]         # ⑦ 欄位比對＋自己寫的（套件.模組:名字；函式或 PipelinePlugin）
      suggest: [false_positive, missed]                   # ⑨ 只提這兩種建議

看全部：`python3 -m main detector blocks`（每個外掛的說明與 control panel 宣告的參數）。
"""
from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass

from core import (PipelinePlugin, get_pipeline_control_panel_registry, get_pipeline_metadata_registry, get_pipeline_run_registry,
                  pipeline_key, registered)


class Registered(Mapping):
    """一段登記了的外掛：{名字: 說明}，照登記順序；跟 dict 一樣可以 in、迭代、items。"""

    def __init__(self, stage: str):
        self.stage = stage

    def __iter__(self):
        return iter(registered(self.stage))

    def __len__(self):
        return len(registered(self.stage))

    def __getitem__(self, name: str) -> str:
        meta = get_pipeline_metadata_registry().get(pipeline_key(self.stage, name))
        if meta is None:
            raise KeyError(name)
        return meta.description


@dataclass(frozen=True)
class Stage:
    key: str                                   # 情境資料檔 blocks: 底下的鍵
    title: str                                 # 給人看的（模組編號＋名稱）
    contract: str                              # 這一段外掛的 run 長什麼樣
    one: bool = False                          # True：只能選一個（併件）
    fallback_attr: str | None = None           # 情境沒寫 blocks 時改看 Scenario 的這個屬性（報告段落沿用 sections）
    commands: tuple[tuple[str, str], ...] = ()  # 內建名字 → 對應的指令（④ 的外掛是指令；detector blocks 列出來）

    @property
    def builtin(self) -> Registered:
        return Registered(self.key)

    def defaults(self) -> list[str]:
        return list(self.builtin)

    def enabled(self, scenario) -> list[str]:
        """這個情境開了哪幾個（照順序）。寫錯名字丟 ValueError，講清楚有哪些。"""
        got = (getattr(scenario, "blocks", None) or {}).get(self.key)
        if got is None and self.fallback_attr:
            got = getattr(scenario, self.fallback_attr, None)
        names = self.defaults() if got is None else [str(x) for x in got]
        known = set(self.builtin)
        bad = [n for n in names if n not in known and ":" not in n]
        if bad:
            raise ValueError(f"blocks.{self.key} 不認得 {'、'.join(bad)}：登記了的有 {'、'.join(self.builtin) or '（沒有，入口沒登記 MainPreset？）'}；"
                             f"自己寫的用「套件.模組:名字」")
        if self.one and len(names) != 1:
            raise ValueError(f"blocks.{self.key} 只能選一個（現在是 {names}）")
        return names

    def on(self, scenario, name: str) -> bool:
        return name in self.enabled(scenario)

    def load(self, name: str):
        """名字 → run。登記了的從 run 登記表拿；「套件.模組:名字」import（函式就是 run；PipelinePlugin 用它的 run）。"""
        if name in self.builtin:
            return get_pipeline_run_registry().get(pipeline_key(self.key, name))
        mod, _, attr = name.partition(":")
        try:
            obj = getattr(importlib.import_module(mod), attr)
        except (ImportError, AttributeError, ValueError) as e:
            raise ValueError(f"blocks.{self.key} 的 {name} 載入不了：{e}") from e
        if isinstance(obj, PipelinePlugin):
            return obj.run if obj.run is not None else obj.load_run()
        return obj

    def loaded(self, scenario) -> list[tuple[str, object]]:
        return [(n, self.load(n)) for n in self.enabled(scenario)]


STAGES: dict[str, Stage] = {s.key: s for s in (
    Stage("detect", "④ 判斷",
          "內建的是指令（關掉的指令跑了只印一行、回 0）；自己寫的是 register(app)：在 detector 加自己的子命令，跟情境的 commands 一樣",
          commands=(("dynamic_baseline", "train、detect、tune"), ("peer_compare", "peer"), ("threshold_rule", "rules"),
                    ("chronic_issue", "chronic"), ("quality_check", "quality"), ("level_shift", "shift"),
                    ("trace_latency", "trace-collect、trace-train、trace-detect"))),
    Stage("grouping", "⑥ 收斂：併件",
          "merge(open_alerts, anomalies, now, close_after, done_ids, calls, links, scenario) -> list[Alert]", one=True),
    Stage("severity", "⑥ 收斂：嚴重度",
          "fn(ctx: SeverityContext, g: dict | None) -> dict：一串依序跑，前一個的結果是下一個的 g（第一個收到 None）；g 要有 severity、why"),
    Stage("rca_sections", "⑦ 根因分析：報告段落",
          "fn(c: Ctx) -> None：往 c.out 加 Markdown 的行、往 c.findings 放結構化的結果", fallback_attr="sections"),
    Stage("similar", "⑦ 根因分析：相似事故",
          "fn(repo, fp, text, exclude_id, opts) -> list[(事故 id, 文件, 向量相似度或 None)]：候選，分數照舊由欄位比對＋相似度算"),
    Stage("summary", "⑦ 根因分析：摘要",
          "fn(al, findings, similar, alerts_config, unit) -> {ok, text, provider…} 或 None"),
    Stage("hints", "⑦ 根因分析：歷史 case",
          "fn(findings, cases, cls, scenario) -> list[str]：報告最前面多的幾行（Markdown）；cases 是確認過的事故 [(id, 文件)]"),
    Stage("suggest", "⑨ 回饋：調整建議",
          "fn(ctx: SuggestContext) -> int：提了幾筆（寫進 aiops_tuning，status proposed）；from_text 是送出回饋時跑的，不在 feedback suggest 裡"),
)}


def stage(key: str) -> Stage:
    return STAGES[key]


def enabled(key: str, scenario) -> list[str]:
    return STAGES[key].enabled(scenario)


def on(key: str, scenario, name: str) -> bool:
    return STAGES[key].on(scenario, name)


def describe(scenario) -> list[str]:
    """`detector blocks` 印的：每一段登記了哪些外掛、這個情境開了哪幾個、每個外掛的 control panel 參數。"""
    lines = []
    for s in STAGES.values():
        try:
            on_, err = s.enabled(scenario), ""
        except ValueError as e:
            on_, err = [], str(e)
        commands = dict(s.commands)
        lines.append(f"{s.title}（blocks.{s.key}{'，只能選一個' if s.one else ''}）")
        for name, desc in s.builtin.items():
            cmd = f"　指令：{commands[name]}" if name in commands else ""
            lines.append(f"  {'✓' if name in on_ else '·'} {name:18} {desc}{cmd}")
            panel = get_pipeline_control_panel_registry().get(pipeline_key(s.key, name))
            lines += [f"      · {line}" for line in (panel.describe() if panel else [])]
        for name in on_:
            if name not in s.builtin:
                lines.append(f"  ✓ {name:18} （自己寫的）")
        if err:
            lines.append(f"  ⚠ {err}")
        lines.append(f"    自己寫的外掛：{s.contract}")
    return lines
