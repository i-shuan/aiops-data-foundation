"""④ 接收器（工廠版）：別人判好的異常 → aiops-anomalies 的格式（docs/pdca/meta/2026-11-factory-engine.md）。

工廠的異常由 ML 團隊的模型（異常跳點）和 FDC 鳴叫系統判，engine 不再自己判 FDC 感測值；這裡只把它們轉成 ⑤ 異常總表的一筆，
後面的收斂、根因分析、通知、回饋全部照舊。設定（configs/factory/ingest.yaml）一個來源一筆 input：

  inputs:
    - name: ml                              # 來源名，寫進 anomaly 的 source
      index: factory-ml-anomalies-*         # 讀哪裡（ES 索引；source: file 時是 files 的 key）
      time_field: "@timestamp"
      filter: {term: {is_anomaly: true}}    # 選填：只收符合的列（ES 查詢的寫法）
      id_field: run_id                      # 選填：來源的識別碼，重讀不重複；沒有就用時間＋對象＋序列＋值算
      series: "fdc.{parameter}"             # 序列名；{欄位} 帶入該列的值
      keys: {equipment: equipment_id, chamber: chamber_id, recipe: recipe_id, step: step_id, lot: lot_id, parameter: parameter}
      value: value                          # 當時的值（欄位名）
      expected: expected                    # 選填 → median
      score: score                          # 選填 → z（ML 的分數；越大越離群）
      level: ml                             # anomaly 的 level：ml、fdc（不是 engine 自己的 dow_hour／rule／peer）
      status: anomaly                       # anomaly（可以自己開一件）或 warning（只併進已開著的件）；或 {field, map, default}
      severity: {field: alarm_level, map: {OOS: critical, OOC: warning}, default: warning}   # 或固定字串
      direction: up                         # 選填：up／down／both；或 {field, map, default}
      message: "{equipment_id} {chamber_id} {parameter}={value}（score {score}）"           # 選填：給人看的一句話

keys 的名字（equipment、chamber、recipe、step、lot）是後面收斂、根因分析認的（investigator/alerts.py 的 SVC_KEYS、route_of）；
右邊是來源的欄位名，拿到真實樣本換右邊就好。這裡只做轉換（單元測試用）；讀來源、寫 ES 在 cmd_ingest.py。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pandas as pd

from shared.config.ingest import IngestConfig, Input
from shared.infra.es import doc_id, get_field

LEVELS = ("ml", "fdc", "external")
_FIELD = re.compile(r"\{([^{}]+)\}")


def _fmt(template: str, row: dict) -> str:
    """"fdc.{parameter}" → 把 {欄位} 換成該列的值；沒有的欄位留空字串。"""
    def rep(m):
        v = get_field(row, m.group(1))
        if isinstance(v, float):
            return f"{v:.4g}"
        return "" if v is None else str(v)
    return _FIELD.sub(rep, template)


def _pick(row: dict, spec, default=None):
    """固定值（字串），或 {field, map, default}：讀該列的欄位、查表對應。"""
    if spec is None:
        return default
    if isinstance(spec, str):
        return spec
    v = get_field(row, spec["field"])
    m = spec.get("map") or {}
    if v is None:
        return spec.get("default", default)
    return m.get(str(v), m.get(v, spec.get("default", default) if m else v))


def _num(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(v) else v


def _ts(x) -> datetime | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("UTC").floor("us").to_pydatetime()


def to_anomaly(row: dict, inp: Input, detected_at: datetime) -> tuple[str, dict] | None:
    """一列來源 → (_id, aiops-anomalies 的文件)。沒有時間、沒有對象（keys 全空）、沒有值的列回 None（呼叫端數掉了幾筆）。"""
    t = _ts(get_field(row, inp.time_field))
    if t is None:
        return None
    keys = {k: v for k, f in inp.key_names.items() if (v := get_field(row, f)) not in (None, "") and not (isinstance(v, float) and pd.isna(v))}
    if not keys:
        return None
    keys = {k: (str(v) if not isinstance(v, (int, float)) or isinstance(v, bool) else v) for k, v in keys.items()}
    value = _num(get_field(row, inp.value)) if inp.value else None
    series = _fmt(inp.series, row) if "{" in inp.series else inp.series
    level = inp.level
    status = _pick(row, inp.status, "anomaly")
    if status not in ("anomaly", "warning"):
        status = "anomaly"
    severity = _pick(row, inp.severity, "warning")
    if severity not in ("warning", "high", "critical"):
        severity = "warning"
    src_id = get_field(row, inp.id_field) if inp.id_field else None
    name = inp.name
    _id = doc_id("ingest", name, str(src_id)) if src_id not in (None, "") else doc_id("ingest", name, t.isoformat(), keys, series, value)
    doc = {"@timestamp": t.isoformat(), "series": series, "keys": keys, "value": value,
           "median": _num(get_field(row, inp.expected)) if inp.expected else None,
           "expected_low": None, "expected_high": None,
           "z": _num(get_field(row, inp.score)) if inp.score else None,
           "level": level, "direction": _pick(row, inp.direction, "up"), "status": status, "severity": severity,
           "detected_at": detected_at.isoformat(), "source": name, "source_id": None if src_id in (None, "") else str(src_id)}
    if inp.message:
        doc["message"] = _fmt(inp.message, row)
    else:
        what = "、".join(f"{k} {v}" for k, v in keys.items() if k != "lot")
        doc["message"] = f"{what}：{series}" + (f" {value:.4g}" if value is not None else "") + (f"（score {doc['z']:.3g}）" if doc["z"] is not None else "")
    if inp.extra:
        doc["extra"] = {f: v for f in inp.extra if (v := get_field(row, f)) is not None}
    return _id, doc


def convert(rows: list[dict], inp: Input, detected_at: datetime | None = None) -> tuple[list[tuple[str, dict]], int]:
    """一批列 → (文件清單, 略過幾筆)。同一個 _id 後面蓋前面（來源重送）。"""
    detected_at = detected_at or datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    skipped = 0
    for r in rows:
        got = to_anomaly(r, inp, detected_at)
        if got is None:
            skipped += 1
            continue
        out[got[0]] = got[1]
    return list(out.items()), skipped


def validate(ingest_config: IngestConfig) -> list[str]:
    """模型管不到的：每個 input 的 name 不能重複、keys 不能是空的、至少要有一個 input。name、index、series、keys 有沒有寫、
    level 對不對，設定檔一讀進來模型就擋了（shared/config/ingest.py）。回傳問題清單（空的就是沒問題）。"""
    bad = []
    names = set()
    for i, inp in enumerate(ingest_config.inputs):
        where = f"inputs[{i}]（{inp.name}）"
        if not inp.key_names:
            bad.append(f"{where} 少了 keys")
        if inp.name in names:
            bad.append(f"{where} name 重複")
        names.add(inp.name)
    if not ingest_config.inputs:
        bad.append("沒有 inputs")
    return bad
