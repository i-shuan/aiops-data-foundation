"""設定模型的預設值跟程式裡真正用的預設值是同一份：Train／Detect 的欄位預設 == dynamic_baseline 的 TrainOptions／DetectOptions。
（yaml 沒寫就是預設值；兩邊不一樣的話，yaml 瘦身後行為會悄悄變。）"""
import dataclasses
import unittest

from detect.config.types import Detect, Train
from detect.strategy.dynamic_baseline import DetectOptions, TrainOptions


def defaults(model) -> dict:
    return {n: f.default for n, f in model.model_fields.items()}


class SameDefaults(unittest.TestCase):
    def test_train(self):
        self.assertEqual(defaults(Train), {f.name: f.default for f in dataclasses.fields(TrainOptions)})

    def test_detect(self):
        self.assertEqual(defaults(Detect), {f.name: f.default for f in dataclasses.fields(DetectOptions)})

    def test_options_built_from_an_empty_section_equal_the_dataclass_defaults(self):
        self.assertEqual(TrainOptions(**Train().model_dump()), TrainOptions())
        self.assertEqual(DetectOptions(**Detect().model_dump()), DetectOptions())


if __name__ == "__main__":
    unittest.main()
