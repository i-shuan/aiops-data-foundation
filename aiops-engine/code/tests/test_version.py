"""core.version：engine_version 從環境變數 ENGINE_VERSION 來（chart 填 image tag），沒設是 dev。"""
import importlib
import os
import unittest
from unittest import mock

from core import version as V


class EngineVersion(unittest.TestCase):
    def test_from_env(self):
        with mock.patch.dict(os.environ, {"ENGINE_VERSION": "0.3.1"}):
            self.assertEqual(importlib.reload(V).ENGINE_VERSION, "0.3.1")
        importlib.reload(V)

    def test_default_dev(self):
        env = {k: v for k, v in os.environ.items() if k != "ENGINE_VERSION"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(importlib.reload(V).ENGINE_VERSION, "dev")
        with mock.patch.dict(os.environ, {**env, "ENGINE_VERSION": ""}, clear=True):   # chart 給空字串也不要寫空的
            self.assertEqual(importlib.reload(V).ENGINE_VERSION, "dev")
        importlib.reload(V)


if __name__ == "__main__":
    unittest.main()
