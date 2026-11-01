"""這一版 engine 的識別。每筆異常寫入時帶 engine_version（detector/owner.py），之後比較兩版的偵測結果不用靠時間推。"""
import os

# chart 的 ConfigMap 把 image tag 放進 ENGINE_VERSION（helmchart/templates/env.yaml）；本機、測試沒設就是 dev
ENGINE_VERSION = os.environ.get("ENGINE_VERSION") or "dev"
