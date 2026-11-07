"""執行環境：引擎版本、專案根目錄。"""
from pathlib import Path

# chart 的 ConfigMap 把 image tag 放進 ENGINE_VERSION（helmchart/templates/env.yaml）；本機、測試沒設就是 dev。每筆異常寫入時帶上。
from shared.config.settings import engine_version

ENGINE_VERSION = engine_version()
# aiops-engine（src/ 的上一層）：configs/、fake-data/ 都以它為準（子行程在這裡跑；api 讀 series.yaml 也從這裡）
CODE_ROOT = Path(__file__).resolve().parents[3]
