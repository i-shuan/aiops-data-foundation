"""configs/ 底下每份 yaml 載入後的完整內容（含預設值）跟快照一樣。
瘦身（把跟預設值相同的鍵從 yaml 拿掉、把共通值搬進模型預設值）不能改變任何情境載入後的結果；
真的要改某個值：先改 yaml／模型，確認是要的，再 `python3 -m tests.test_config_snapshot --update` 重寫快照。"""
import glob
import json
import os
import pathlib
import sys
import unittest

from common.config.loader import load_config

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAP = ROOT / "tests" / "fixtures" / "config_snapshot.json"


def current() -> dict:
    out = {}
    for f in sorted(glob.glob(str(ROOT / "configs" / "*" / "*.yaml"))):
        rel = os.path.relpath(f, ROOT).replace(os.sep, "/")
        out[rel] = load_config(f, os.path.basename(f)[:-5]).model_dump(mode="json", by_alias=True)
    return out


class ConfigSnapshot(unittest.TestCase):
    def test_every_yaml_loads_to_the_same_content(self):
        want = json.loads(SNAP.read_text(encoding="utf-8"))
        got = current()
        self.assertEqual(sorted(got), sorted(want), "configs/ 的檔案增減了：更新快照")
        for name in want:
            with self.subTest(name):
                self.assertEqual(got[name], want[name])


if __name__ == "__main__":
    if "--update" in sys.argv:
        SNAP.write_text(json.dumps(current(), ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        print("快照已更新")
    else:
        unittest.main()
