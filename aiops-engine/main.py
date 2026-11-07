"""python3 -m main <角色> …：engine 唯一的命令列入口（Dockerfile 的 ENTRYPOINT 是 python3 -m，排程表、helm 的 args 第一個字都是 main；程式在 src/，這裡先把它加進 import 路徑）。

  python3 -m main detector     <子命令> …   ②③④⑤ 偵測、⑨ 回饋、調整、回測、量化（interface/cli/detect.py）
  python3 -m main investigator <子命令> …   ⑥⑦ 事件整合、根因分析（interface/cli/rca.py）
  python3 -m main scheduler    --config …   排程器／beat（interface/jobs/local.py）
  python3 -m main tasks worker --queue …    佇列 worker（interface/jobs/worker.py）
  python3 -m main api                       FastAPI（interface/web/__main__.py）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))   # 程式在 src/；從 aiops-engine/ 跑，不用設 PYTHONPATH

from common.config.settings import MissingSetting, SettingsError, settings
from presets import setup_plugins

ROLE_NAMES = ("detector", "investigator", "scheduler", "tasks", "api")


def _role(name: str):
    """角色的程式到要跑的那一刻才 import：有些模組載入時就會讀環境變數（Celery 的 broker），環境變數不對時要先走到下面的
    try 才能印一行錯誤，不是在 import 就丟 traceback。"""
    if name == "detector":
        from interface.cli.detect import main
        return main
    if name == "investigator":
        from interface.cli.rca import main
        return main
    if name == "scheduler":
        from interface.jobs.local import main
        return main
    if name == "api":
        from interface.web.__main__ import main
        return lambda argv: main()
    from interface.jobs.worker import worker_main

    def tasks(argv: list[str]) -> int:
        if not argv or argv[0] != "worker":
            print("用法：python3 -m main tasks worker --queue <realtime|alert|batch> [--concurrency N]", file=sys.stderr)
            return 2
        return worker_main(argv[1:])
    return tasks


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ROLE_NAMES:
        print(f"用法：python3 -m main <{'|'.join(ROLE_NAMES)}> …", file=sys.stderr)
        return 2
    try:
        setup_plugins(settings().aiops_plugin)     # 對應 Superset 的 setupPlugins()：照情境登記 Preset（presets/）
        return int(_role(argv[0])(argv[1:]) or 0)
    except (SettingsError, MissingSetting) as e:     # 環境變數的問題：一行講清楚，不丟 traceback
        print(f"設定錯誤：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
