"""helmchart/：mode single render 出來是一隻 pod 兩個 container（scheduler＋api）；mode distributed 是 api、beat、三個 worker、redis
（docs/pdca/meta/2026-11-api-beat-worker.md）。資源、探測、設定檔覆蓋掛對位置。要有 helm；沒有就跳過。
排程本身（Forbid、逾時、重試）在 configs/schedule/schedule.yaml，由 test_scheduler 驗；佇列由 test_tasks 驗。"""
import os
import shutil
import subprocess
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.join(HERE, "..", "helmchart")
GRAFANA = os.path.join(HERE, "..", "..", "grafana")


def render(*args) -> dict[str, dict]:
    out = subprocess.run(["helm", "template", "aiops", CHART, *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return {f"{d['kind']}/{d['metadata']['name']}": d for d in yaml.safe_load_all(out) if d}


def containers(docs, deployment="aiops-engine") -> dict[str, dict]:
    return {c["name"]: c for c in docs[f"Deployment/{deployment}"]["spec"]["template"]["spec"]["containers"]}


@unittest.skipUnless(shutil.which("helm"), "要 helm")
class EngineChart(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()

    def test_one_pod_two_containers_no_cronjob(self):
        # Secret ×2：aiops-llm（LLM key）、aiops-api（API token），沒填就是空的
        self.assertEqual(sorted(k.split("/")[0] for k in self.docs), ["ConfigMap", "Deployment", "Secret", "Secret", "Service"])
        dep = self.docs["Deployment/aiops-engine"]["spec"]
        self.assertEqual((dep["replicas"], dep["strategy"]["type"]), (1, "Recreate"))
        self.assertEqual(sorted(containers(self.docs)), ["api", "scheduler"])
        self.assertNotIn("--enqueue", containers(self.docs)["scheduler"]["args"])          # single：排程器自己跑
        self.assertNotIn("AIOPS_BROKER_URL", self.docs["ConfigMap/aiops-env"]["data"])     # 沒有佇列

    def test_scheduler_runs_stage1_by_default_and_stage2_on_demand(self):
        args = containers(self.docs)["scheduler"]["args"]
        self.assertEqual(args[:4], ["main", "scheduler", "--config", "configs/schedule/schedule.yaml"])
        self.assertEqual(args[args.index("--stage") + 1], "1")
        self.assertEqual(containers(render("--set", "scheduler.stage=2"))["scheduler"]["args"][5], "2")

    def test_scheduler_liveness_watches_alive_file(self):
        c = containers(self.docs)["scheduler"]
        self.assertIn("/tmp/scheduler-alive", c["args"])
        self.assertIn("/tmp/scheduler-alive", " ".join(c["livenessProbe"]["exec"]["command"]))
        self.assertIn({"name": "tmp", "mountPath": "/tmp"}, c["volumeMounts"])

    def test_memory_covers_train_and_api_is_separate(self):
        c = containers(self.docs)
        self.assertEqual(c["scheduler"]["resources"]["limits"]["memory"], "6Gi")
        self.assertEqual(c["api"]["resources"]["limits"]["memory"], "512Mi")
        self.assertEqual(c["api"]["livenessProbe"]["httpGet"]["path"], "/health")
        self.assertEqual(c["api"]["args"], ["main", "api"])
        self.assertIn({"name": "API_PORT", "value": "8080"}, c["api"]["env"])
        self.assertEqual(self.docs["Service/aiops-api"]["spec"]["ports"][0]["port"], 8080)
        self.assertEqual(self.docs["Secret/aiops-api"]["stringData"], {"AIOPS_API_TOKEN": ""})
        self.assertEqual(render("--set", "api.token=abc")["Secret/aiops-api"]["stringData"]["AIOPS_API_TOKEN"], "abc")

    def test_engine_version_is_the_image_tag(self):
        # 每筆異常帶的 engine_version（core/version.py）要跟跑的 image 是同一個 tag，兩個 container 都從 aiops-env 拿
        env = self.docs["ConfigMap/aiops-env"]["data"]
        self.assertEqual(env["ENGINE_VERSION"], "0.1.0")
        self.assertEqual(render("--set", "image.tag=0.3.1")["ConfigMap/aiops-env"]["data"]["ENGINE_VERSION"], "0.3.1")
        for c in containers(self.docs).values():
            self.assertIn({"configMapRef": {"name": "aiops-env"}}, c["envFrom"])

    def test_api_can_be_turned_off(self):
        docs = render("--set", "api.enabled=false")
        self.assertEqual(list(containers(docs)), ["scheduler"])
        self.assertNotIn("Service/aiops-api", docs)

    def test_bad_mode_fails(self):
        with self.assertRaises(subprocess.CalledProcessError):
            render("--set", "mode=weird")

    def test_config_override_mounts_in_place_for_both_containers(self):
        docs = render("--set", "configOverrides.schedule.schedule=timezone: UTC")
        self.assertEqual(list(docs["ConfigMap/aiops-configs"]["data"]), ["schedule__schedule.yaml"])
        for name, c in containers(docs).items():
            mount = next(m for m in c["volumeMounts"] if m["name"] == "configs")
            self.assertEqual((mount["mountPath"], mount["subPath"]), ("/app/configs/schedule/schedule.yaml", "schedule__schedule.yaml"), name)
        self.assertEqual([m["name"] for m in containers(self.docs)["scheduler"]["volumeMounts"]], ["tmp"])


@unittest.skipUnless(shutil.which("helm"), "要 helm")
class DistributedChart(unittest.TestCase):
    """mode: distributed——Superset 式：api、beat（只丟）、worker 依佇列、redis；alert worker 只能一個。"""
    @classmethod
    def setUpClass(cls):
        cls.docs = render("--set", "mode=distributed")

    def test_roles(self):
        deps = sorted(k.split("/")[1] for k in self.docs if k.startswith("Deployment/"))
        self.assertEqual(deps, ["aiops-api", "aiops-beat", "aiops-redis", "aiops-worker-alert", "aiops-worker-batch", "aiops-worker-realtime"])
        self.assertNotIn("Deployment/aiops-engine", self.docs)
        self.assertEqual(sorted(k.split("/")[1] for k in self.docs if k.startswith("Service/")), ["aiops-api", "aiops-redis"])
        self.assertEqual(self.docs["ConfigMap/aiops-env"]["data"]["AIOPS_BROKER_URL"], "redis://aiops-redis:6379/0")

    def test_beat_only_enqueues_and_is_single(self):
        beat = containers(self.docs, "aiops-beat")["beat"]
        self.assertEqual(beat["args"][:4], ["main", "scheduler", "--config", "configs/schedule/schedule.yaml"])
        self.assertIn("--enqueue", beat["args"])
        self.assertIn("/tmp/scheduler-alive", " ".join(beat["livenessProbe"]["exec"]["command"]))
        dep = self.docs["Deployment/aiops-beat"]["spec"]
        self.assertEqual((dep["replicas"], dep["strategy"]["type"]), (1, "Recreate"))
        self.assertEqual(beat["resources"]["limits"]["memory"], "256Mi")       # 只丟工作，不用 6Gi

    def test_workers_per_queue(self):
        for q, conc in (("realtime", "2"), ("alert", "1"), ("batch", "1")):
            w = containers(self.docs, f"aiops-worker-{q}")["worker"]
            self.assertEqual(w["args"], ["main", "tasks", "worker", "--queue", q, "--concurrency", conc], q)
            self.assertIn({"configMapRef": {"name": "aiops-env"}}, w["envFrom"])
        self.assertEqual(containers(self.docs, "aiops-worker-batch")["worker"]["resources"]["limits"]["memory"], "6Gi")   # train 在這
        self.assertEqual(self.docs["Deployment/aiops-worker-alert"]["spec"]["strategy"]["type"], "Recreate")
        self.assertEqual(render("--set", "mode=distributed", "--set", "workers.realtime.replicas=3")["Deployment/aiops-worker-realtime"]["spec"]["replicas"], 3)

    def test_alert_worker_must_be_single_consumer(self):
        for bad in ("workers.alert.replicas=2", "workers.alert.concurrency=2"):
            with self.assertRaises(subprocess.CalledProcessError, msg=bad):
                render("--set", "mode=distributed", "--set", bad)
        with self.assertRaises(subprocess.CalledProcessError):
            render("--set", "mode=distributed", "--set", "workers.fast.replicas=1")

    def test_external_redis(self):
        docs = render("--set", "mode=distributed", "--set", "redis.enabled=false", "--set", "redis.url=redis://corp-redis:6379/3")
        self.assertNotIn("Deployment/aiops-redis", docs)
        self.assertEqual(docs["ConfigMap/aiops-env"]["data"]["AIOPS_BROKER_URL"], "redis://corp-redis:6379/3")

    def test_api_service_points_at_api_pods(self):
        self.assertEqual(self.docs["Service/aiops-api"]["spec"]["selector"], {"app.kubernetes.io/name": "aiops-api"})
        self.assertEqual(render("--set", "mode=distributed", "--set", "api.replicas=2")["Deployment/aiops-api"]["spec"]["replicas"], 2)

    def test_plugin_picks_schedule_for_beat(self):
        beat = containers(render("--set", "mode=distributed", "--set", "plugin=factory"), "aiops-beat")["beat"]
        self.assertEqual(beat["args"][2], "configs/factory/schedule.yaml")

    def test_config_override_mounts_in_every_role(self):
        docs = render("--set", "mode=distributed", "--set", "configOverrides.schedule.schedule=timezone: UTC")
        for dep in ("aiops-api", "aiops-beat", "aiops-worker-realtime", "aiops-worker-alert", "aiops-worker-batch"):
            c = next(iter(containers(docs, dep).values()))
            mount = next(m for m in c["volumeMounts"] if m["name"] == "configs")
            self.assertEqual(mount["mountPath"], "/app/configs/schedule/schedule.yaml", dep)


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
            g = lambda *a: list(yaml.safe_load_all(subprocess.run(["helm", "template", "g", os.path.join(GRAFANA, "helmchart"), *a],
                                                                    capture_output=True, text=True, encoding="utf-8", check=True).stdout))
            docs = g("--set", "plugin.enabled=true")
            dep = next(d for d in docs if d and d["kind"] == "Deployment")
            vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
            self.assertIn("emptyDir", vols["grafana-plugins"])
            dash = next(d for d in docs if d and d["kind"] == "ConfigMap" and d["metadata"]["name"] == "grafana-dashboards")["data"]
            self.assertIn("aiops-feedback-forms.json", dash)                    # 外掛開：多載入外掛表單版
            off = g()                                                            # 預設：不裝外掛、沒有 initContainer、只有 HTML 連結版
            dep = next(d for d in off if d and d["kind"] == "Deployment")
            self.assertNotIn("initContainers", dep["spec"]["template"]["spec"])
            dash = next(d for d in off if d and d["kind"] == "ConfigMap" and d["metadata"]["name"] == "grafana-dashboards")["data"]
            self.assertNotIn("aiops-feedback-forms.json", dash)
            self.assertIn("/feedback?alert=${alert}", dash["aiops-feedback.json"])
            self.assertNotIn("http://localhost:18080", g("--set", "feedbackUrl=https://aiops.example")[0] and
                             next(d for d in g("--set", "feedbackUrl=https://aiops.example") if d and d["kind"] == "ConfigMap" and d["metadata"]["name"] == "grafana-dashboards")["data"]["aiops-feedback.json"])
