"""data/source：OTel、APM 各一個來源模組。為了確認三件事：
1. 登記表與選法：依名字取、依索引認領、宣告的來源是預設。
2. 兩種來源對「同一個請求」轉出同一種統一 span（後面的合成、判斷、根因不認得來源）。
3. 來源判斷只在 data/source（別處不能再寫「是不是 apm」）。"""
import ast
import pathlib
import random
import unittest
from datetime import datetime, timezone

from data.source import ALIASES, FORMATS, DEFAULT, for_index, format_of, to_span
from data.source.apm import ApmFormat
from data.source.otel import OtelFormat
from data.store.models import schema_of, trace_id_field
from tests import apm_synth

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


class Registry(unittest.TestCase):
    def test_names(self):
        self.assertEqual(sorted(FORMATS), ["apm", "otel"])
        self.assertIsInstance(format_of("apm"), ApmFormat)
        self.assertIsInstance(format_of("otel"), OtelFormat)
        with self.assertRaises(ValueError) as cm:
            format_of("jaeger")
        self.assertIn("jaeger", str(cm.exception))

    def test_for_index_apm_claims_its_own_indices(self):
        self.assertEqual(for_index("traces-apm-*", "otel").name, "apm")            # 索引名說了算，不管宣告什麼
        self.assertEqual(for_index("traces-apm-default", "otel").name, "apm")

    def test_for_index_falls_back_to_the_declared_source(self):
        self.assertEqual(for_index("traces-generic.otel-*", "otel").name, "otel")
        self.assertEqual(for_index("traces-generic.otel-*", "apm").name, "apm")    # 宣告 apm 時，其他 raw 索引也當 APM（跟抽出來之前一樣）
        self.assertEqual(for_index("traces-whatever-*").name, DEFAULT)
        # 工廠情境宣告的來源是 "factory"（MES 履歷，沒有 raw trace）：不在登記表裡也不能丟例外，走預設
        self.assertEqual(for_index("lot-paths-fab1-*", "factory").name, DEFAULT)
        self.assertEqual(trace_id_field("lot-paths-fab1-*", "factory"), "trace_id")

    def test_schema_of_keeps_the_old_answers(self):
        self.assertEqual([schema_of(i, "factory") for i in ("lot-paths-fab1-2026.11.07", "traces-merged-mes-2026.11.07", "traces-apm-default", "traces-generic.otel-it")],
                         ["merged", "merged", "apm", "otel"])

    def test_aliases_come_from_the_sources(self):
        self.assertIn(("service", "resource.attributes.service.name", "entry.service"), ALIASES)
        self.assertEqual(sorted(a[0] for a in ApmFormat.aliases), [])             # APM 目前沒有；之後加在 apm.py


def _otel_of(d: dict) -> dict:
    """把 apm_synth 造的 APM 文件換成同一個 span 的 OTel 文件（Collector 的寫法）。"""
    ms = (d.get("transaction.duration.us") or d.get("span.duration.us")) / 1000.0
    is_tx = d["processor.event"] == "transaction"
    attrs = {"peer.service": d["span.destination.service.resource"]} if not is_tx else {}
    return {"@timestamp": d["@timestamp"], "trace_id": d["trace.id"], "span_id": d["transaction.id"] if is_tx else d["span.id"],
            "parent_span_id": d.get("parent.id"), "name": d["transaction.name"] if is_tx else d["span.name"],
            "kind": "Server" if is_tx else "Client", "duration": ms * 1e6, "status.code": "Error" if d["event.outcome"] == "failure" else "Unset",
            "resource": {"attributes": {"service.name": d["service.name"]}}, "attributes": attrs}


class SameUnifiedSpan(unittest.TestCase):
    def test_one_call_chain_in_both_formats(self):
        docs, _ = apm_synth.trace_docs(random.Random(3), datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc), True, 1)
        apm = [to_span(apm_synth.nest(d)) for d in docs]
        otel = [to_span(_otel_of(d)) for d in docs]
        for a, o in zip(apm, otel):
            with self.subTest(name=a["name"]):
                for k in ("trace_id", "span_id", "parent", "service", "name", "kind", "target", "error"):
                    self.assertEqual(a[k], o[k], k)
                self.assertAlmostEqual(a["dur_ms"], o["dur_ms"], places=2)
        self.assertEqual([s["service"] for s in apm], ["mes-api", "mes-api", "wip-worker", "wip-worker"])
        self.assertEqual([s["kind"] for s in apm], ["Server", "Client", "Server", "Client"])


class OnlyTheSourceFolderKnowsTheSource(unittest.TestCase):
    ALLOWED = ("data/source/", "common/config/settings.py", "common/scenario/base.py")     # 宣告來源的地方（環境變數、情境資料檔）

    def test_no_apm_otel_literals_or_helpers_elsewhere(self):
        bad = []
        for p in SRC.rglob("*.py"):
            rel = p.relative_to(SRC).as_posix()
            if rel.startswith(self.ALLOWED[0]) or rel in self.ALLOWED:
                continue
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Constant) and n.value in ("apm", "otel"):
                    bad.append(f"{rel}:{n.lineno} 字串 {n.value!r}")
                if isinstance(n, ast.Name) and n.id in ("_apm_span", "is_apm_doc", "_APM_FIELDS", "_SPAN_FIELDS"):
                    bad.append(f"{rel}:{n.lineno} {n.id}")
        self.assertEqual(bad, [], "來源判斷只在 data/source：要新增或改欄位對照，改那裡的 SourceFormat")


if __name__ == "__main__":
    unittest.main()
