"""shared/config/settings.py：環境變數集中成 Settings。程式裡只有它讀 os.environ；預設值照原本；型別錯一行講清楚；必填的要用才檢查。"""
import ast
import pathlib
import unittest

from shared.config.settings import MissingSetting, Settings, SettingsError, settings

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


class OnlySettingsReadsEnv(unittest.TestCase):
    def test_no_os_environ_elsewhere(self):
        bad = []
        for p in list(SRC.rglob("*.py")) + [SRC.parent / "main.py"]:
            if "__pycache__" in p.parts or p.name == "settings.py" and p.parent.name == "config":
                continue
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os" and n.attr in ("environ", "getenv"):
                    bad.append(f"{p.relative_to(SRC.parent)}:{n.lineno}")
        self.assertEqual(bad, [], "只有 shared/config/settings.py 可以讀環境變數，其他地方用 settings()")


class Defaults(unittest.TestCase):
    def test_same_as_before(self):
        s = settings({})
        self.assertEqual((s.llm_provider, s.aiops_plugin, s.aiops_source, s.api_port, s.schedule_stage, s.engine_version, s.tz),
                         ("template", "observability", "otel", 8080, 1, "dev", "Asia/Taipei"))
        self.assertIsNone(s.es_url)
        self.assertEqual(s.cors_origins, set())
        self.assertIsNone(s.tls)

    def test_blank_is_unset(self):
        """chart 會給空字串（ENGINE_VERSION、LLM_PROVIDER…），當沒設。"""
        s = settings({"ENGINE_VERSION": "", "LLM_PROVIDER": "", "API_PORT": "", "ES_API_KEY": ""})
        self.assertEqual((s.engine_version, s.llm_provider, s.api_port, s.es_api_key), ("dev", "template", 8080, None))

    def test_values(self):
        s = settings({"API_PORT": "9000", "SCHEDULE_STAGE": "2", "API_CORS_ORIGIN": "https://a.example/, https://b.example",
                      "API_TLS_CERT": "c.pem", "API_TLS_KEY": "k.pem", "LLM_PROVIDER": "openai"})
        self.assertEqual((s.api_port, s.schedule_stage, s.llm_provider), (9000, 2, "openai"))
        self.assertEqual(s.cors_origins, {"https://a.example", "https://b.example"})
        self.assertEqual(s.tls, ("c.pem", "k.pem"))

    def test_tls_needs_both(self):
        self.assertIsNone(settings({"API_TLS_CERT": "c.pem"}).tls)

    def test_chat_model_default_depends_on_provider(self):
        self.assertEqual(settings({}).chat_model("gpt-4o-mini"), "gpt-4o-mini")
        self.assertEqual(settings({"LLM_CHAT_MODEL": "x"}).chat_model("gpt-4o-mini"), "x")


class Errors(unittest.TestCase):
    def test_bad_int_names_the_variable(self):
        with self.assertRaises(SettingsError) as cm:
            settings({"API_PORT": "abc"})
        self.assertIn("API_PORT", str(cm.exception))
        self.assertIn("'abc'", str(cm.exception))

    def test_required_only_when_used(self):
        s = settings({})            # 沒設 ES_URL，建立 Settings 不會炸
        with self.assertRaises(MissingSetting) as cm:
            s.require_es_url()
        self.assertIsInstance(cm.exception, KeyError)     # 原本就是 KeyError
        self.assertIn("ES_URL", str(cm.exception))
        with self.assertRaises(MissingSetting):
            s.require_llm_api_key()
        self.assertEqual(settings({"ES_URL": "http://es:9200"}).require_es_url(), "http://es:9200")

    def test_settings_are_frozen(self):
        with self.assertRaises(Exception):
            Settings().api_port = 1


if __name__ == "__main__":
    unittest.main()
