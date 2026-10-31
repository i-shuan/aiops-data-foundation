"""helmchart/：render 出來的 CronJob 要有 Forbid、逾時、對的重試；alert 的時限夠跑真的 LLM（docs/pdca/phase5-learning-loop/2026-10-fix-critical.md 4）。
要有 helm；沒有就跳過。"""
import os
import shutil
import subprocess
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.join(HERE, "..", "..", "helmchart")
GRAFANA = os.path.join(HERE, "..", "..", "..", "grafana")


def render(*args) -> dict[str, dict]:
    out = subprocess.run(["helm", "template", "aiops", CHART, *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return {f"{d['kind']}/{d['metadata']['name']}": d for d in yaml.safe_load_all(out) if d}


@unittest.skipUnless(shutil.which("helm"), "要 helm")
class EngineChart(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stage1 = render()
        cls.all = render("--set", "stage2=true")
        cls.jobs = {k: v for k, v in cls.all.items() if k.startswith("CronJob/")}

    def test_stage1_is_the_minimal_loop(self):
        self.assertEqual(sorted(k for k in self.stage1 if k.startswith("CronJob/")),
                         ["CronJob/aiops-alert", "CronJob/aiops-detect", "CronJob/aiops-rules", "CronJob/aiops-train"])
        self.assertEqual(len(self.jobs), 10)
        self.assertIn("Deployment/aiops-feedback", self.stage1)

    def test_alert_deadline_covers_llm_calls(self):
        # 一件新的：LLM 最多 60 秒逾時（core/llm.py）；一輪至少要能處理好幾件，不然被砍什麼都沒寫
        import inspect
        from core import llm as LLM
        self.assertIn("timeout=60", inspect.getsource(LLM._call_openai))
        spec = self.jobs["CronJob/aiops-alert"]["spec"]
        self.assertGreaterEqual(spec["jobTemplate"]["spec"]["activeDeadlineSeconds"], 5 * 60 + 60)
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")

    def test_every_job_forbids_overlap_and_has_deadline(self):
        for name, d in self.jobs.items():
            self.assertEqual(d["spec"]["concurrencyPolicy"], "Forbid", name)
            self.assertGreater(d["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"], 0, name)
            self.assertLessEqual(d["spec"]["startingDeadlineSeconds"], 300, name)
            self.assertEqual(d["spec"]["timeZone"], "Asia/Taipei", name)

    def test_failed_job_is_retried_within_one_period(self):
        # 每幾分鐘的：下一輪就是重試，Job 內不重試；每天、每週的：不重試就要等明天（train 失敗 → baseline 舊一天）
        # docs/pdca/phase2-anomaly/2026-10-adjust-schedule-verify.md
        for name, d in self.jobs.items():
            frequent = d["spec"]["schedule"].split()[1] == "*"
            self.assertEqual(d["spec"]["jobTemplate"]["spec"]["backoffLimit"], 0 if frequent else 2, name)
        self.assertEqual(self.jobs["CronJob/aiops-train"]["spec"]["jobTemplate"]["spec"]["backoffLimit"], 2)
        self.assertEqual(self.jobs["CronJob/aiops-rules"]["spec"]["jobTemplate"]["spec"]["backoffLimit"], 0)

    def test_train_gets_more_memory(self):
        lim = lambda n: self.jobs[f"CronJob/aiops-{n}"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]
        self.assertEqual(lim("train"), "4Gi")
        self.assertEqual(lim("rules"), "1Gi")

    def test_config_override_mounts_in_place(self):
        docs = render("--set", "configOverrides.baseline.series=a: 1")
        cm = docs["ConfigMap/aiops-configs"]["data"]
        self.assertEqual(list(cm), ["baseline__series.yaml"])
        mount = docs["CronJob/aiops-detect"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]
        self.assertEqual((mount["mountPath"], mount["subPath"]), ("/app/configs/baseline/series.yaml", "baseline__series.yaml"))
        self.assertNotIn("volumeMounts", self.stage1["CronJob/aiops-detect"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0])


if __name__ == "__main__":
    unittest.main()


import hashlib
import tempfile
import zipfile

SCRIPT = os.path.join(GRAFANA, "helmchart", "files", "install-plugin.sh")
SHA = "1eece14d67b2ffac4bbba18f5c69cde3f0f7aecef2276b0f1b33bc3cfb9998d0"


@unittest.skipUnless(all(shutil.which(t) for t in ("sh", "curl", "unzip", "sha256sum")), "要 sh、curl、unzip、sha256sum")
class GrafanaPluginInit(unittest.TestCase):
    """Grafana 的 initContainer（grafana/helmchart/files/install-plugin.sh）：核對 SHA-256、裝進外掛資料夾、已經裝了就跳過
    （docs/pdca/phase5-learning-loop/2026-10-grafana-plugin-init.md）。用 file:// 當作 Nexus 的網址，不連網路。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.zip = os.path.join(self.tmp, "p.zip")
        with zipfile.ZipFile(self.zip, "w") as z:
            z.writestr("demo-panel/plugin.json", '{"id": "demo-panel"}')
            z.writestr("demo-panel/module.js", "x")
        with open(self.zip, "rb") as f:
            self.sha = hashlib.sha256(f.read()).hexdigest()
        self.dir = os.path.join(self.tmp, "plugins")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_script(self, sha=None, url=None, pid="demo-panel"):
        env = {**os.environ, "PLUGIN_ID": pid, "PLUGIN_URL": url or f"file://{self.zip}", "PLUGIN_SHA256": sha or self.sha, "PLUGINS_DIR": self.dir}
        return subprocess.run(["sh", SCRIPT], env=env, capture_output=True, text=True)

    def test_install_then_skip(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "demo-panel", "plugin.json")))
        # 記版本的檔放在外掛資料夾外面：放裡面 Grafana 驗簽章會說檔案被改過
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, "demo-panel"))), ["module.js", "plugin.json"])
        r = self.run_script()
        self.assertEqual(r.returncode, 0)
        self.assertIn("不用重裝", r.stdout)

    def test_bad_checksum_installs_nothing(self):
        r = self.run_script(sha="0" * 64)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SHA-256 不符", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "demo-panel")))

    def test_wrong_plugin_id(self):
        r = self.run_script(pid="other-panel")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("plugin.json", r.stderr)

    def test_missing_file(self):
        r = self.run_script(url=f"file://{self.tmp}/nope.zip")
        self.assertNotEqual(r.returncode, 0)

    def test_chart_and_patch_pin_the_tested_version(self):
        with open(os.path.join(GRAFANA, "helmchart", "values.yaml"), encoding="utf-8") as f:
            plugin = yaml.safe_load(f)["plugin"]
        self.assertEqual((plugin["id"], plugin["sha256"]), ("volkovlabs-form-panel", SHA))
        self.assertIn("6.3.1", plugin["url"])
        with open(os.path.join(GRAFANA, "code", "plugin-init-patch.yaml"), encoding="utf-8") as f:
            init = yaml.safe_load(f)["spec"]["template"]["spec"]["initContainers"][0]
        env = {e["name"]: e["value"] for e in init["env"]}
        self.assertEqual((env["PLUGIN_ID"], env["PLUGIN_SHA256"]), ("volkovlabs-form-panel", SHA))
        self.assertIn("6.3.1", env["PLUGIN_URL"])
        with open(os.path.join(GRAFANA, "code", "INSTALL-form-plugin.md"), encoding="utf-8") as f:
            self.assertIn(SHA, f.read())
        if shutil.which("helm"):   # chart 裝外掛時用 emptyDir：Pod 重建就重裝，版本永遠是 values 寫的
            out = subprocess.run(["helm", "template", "g", os.path.join(GRAFANA, "helmchart")], capture_output=True, text=True, encoding="utf-8", check=True).stdout
            dep = next(d for d in yaml.safe_load_all(out) if d and d["kind"] == "Deployment")
            vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
            self.assertIn("emptyDir", vols["grafana-plugins"])
