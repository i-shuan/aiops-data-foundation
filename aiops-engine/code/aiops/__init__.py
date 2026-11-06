"""AIOps engine：core（連線、entity、repository）／scenario（情境）／service（偵測、收斂、根因、回饋）／interface（命令列、HTTP、排程）。
依賴只往一個方向：interface → service → scenario → core（tests/test_layers.py 鎖著）；docs/pdca/meta/2026-11-target-architecture.md。"""
import os
from pathlib import Path

# chart 的 ConfigMap 把 image tag 放進 ENGINE_VERSION（helmchart/templates/env.yaml）；本機、測試沒設就是 dev。每筆異常寫入時帶上。
ENGINE_VERSION = os.environ.get("ENGINE_VERSION") or "dev"
# aiops-engine/code：configs/、fake-data/ 都以它為準（子行程在這裡跑；api 讀 series.yaml 也從這裡）
CODE_ROOT = Path(__file__).resolve().parent.parent
