"""deploy/：CronJob 的檔案跟產生器一致；alert 的時限夠跑真的 LLM（docs/pdca/2026-10-fix-critical.md 4）。"""
import importlib.util
import os
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY = os.path.join(HERE, "..", "deploy")


def load_gen():
    spec = importlib.util.spec_from_file_location("gen_cronjobs", os.path.join(DEPLOY, "gen-cronjobs.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class CronJobs(unittest.TestCase):
    def setUp(self):
        self.gen = load_gen()
        self.docs = {}
        for name in ("cronjobs.yaml", "cronjobs-stage2.yaml"):
            with open(os.path.join(DEPLOY, name), encoding="utf-8") as f:
                self.docs.update({d["metadata"]["name"]: d for d in yaml.safe_load_all(f) if d})

    def test_files_match_generator(self):
        for name, expected in self.gen.render_all().items():
            with open(os.path.join(DEPLOY, name), encoding="utf-8") as f:
                self.assertEqual(f.read(), expected, f"{name}：改了 gen-cronjobs.py 要重跑它")

    def test_stage1_is_the_minimal_loop(self):
        with open(os.path.join(DEPLOY, "cronjobs.yaml"), encoding="utf-8") as f:
            s1 = sorted(d["metadata"]["name"] for d in yaml.safe_load_all(f) if d)
        self.assertEqual(s1, ["aiops-alert", "aiops-detect", "aiops-rules", "aiops-train"])
        with open(os.path.join(DEPLOY, "kustomization.yaml"), encoding="utf-8") as f:
            k = yaml.safe_load(f)
        self.assertIn("cronjobs.yaml", k["resources"])
        self.assertNotIn("cronjobs-stage2.yaml", k["resources"])     # 第二階段預設不開

    def test_alert_deadline_covers_llm_calls(self):
        # 一件新的：LLM 最多 60 秒逾時（core/llm.py）；一輪至少要能處理好幾件，不然被砍什麼都沒寫
        from core import llm as LLM
        import inspect
        self.assertIn("timeout=60", inspect.getsource(LLM._call_openai))
        spec = self.docs["aiops-alert"]["spec"]
        self.assertGreaterEqual(spec["jobTemplate"]["spec"]["activeDeadlineSeconds"], 5 * 60 + 60)
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")

    def test_every_job_forbids_overlap_and_has_deadline(self):
        for name, d in self.docs.items():
            self.assertEqual(d["spec"]["concurrencyPolicy"], "Forbid", name)
            self.assertGreater(d["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"], 0, name)
            self.assertEqual(d["spec"]["jobTemplate"]["spec"]["backoffLimit"], 0, name)


if __name__ == "__main__":
    unittest.main()
