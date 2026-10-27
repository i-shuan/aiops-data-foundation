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


import hashlib
import shutil
import subprocess
import tempfile
import zipfile

SCRIPT = os.path.join(DEPLOY, "grafana", "install-plugin.sh")
SHA = "1eece14d67b2ffac4bbba18f5c69cde3f0f7aecef2276b0f1b33bc3cfb9998d0"


@unittest.skipUnless(all(shutil.which(t) for t in ("sh", "curl", "unzip", "sha256sum")), "要 sh、curl、unzip、sha256sum")
class GrafanaPluginInit(unittest.TestCase):
    """Grafana 的 initContainer（deploy/grafana/install-plugin.sh）：核對 SHA-256、裝進外掛資料夾、已經裝了就跳過（docs/pdca/2026-10-grafana-plugin-init.md）。
    用 file:// 當作 Nexus 的網址，不連網路。"""

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

    def test_manifests_pin_the_tested_version(self):
        for name in ("plugin-init-patch.yaml", "grafana-example.yaml"):
            with open(os.path.join(DEPLOY, "grafana", name), encoding="utf-8") as f:
                dep = next(d for d in yaml.safe_load_all(f) if d and d["kind"] == "Deployment")
            init = dep["spec"]["template"]["spec"]["initContainers"][0]
            env = {e["name"]: e["value"] for e in init["env"]}
            self.assertEqual(env["PLUGIN_ID"], "volkovlabs-form-panel", name)
            self.assertEqual(env["PLUGIN_SHA256"], SHA, name)
            self.assertIn("6.3.1", env["PLUGIN_URL"], name)
            vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
            self.assertIn("emptyDir", vols["grafana-plugins"], name)     # Pod 重建就重裝，版本永遠是這裡寫的
        with open(os.path.join(HERE, "..", "configs", "grafana", "INSTALL-form-plugin.md"), encoding="utf-8") as f:
            self.assertIn(SHA, f.read())
