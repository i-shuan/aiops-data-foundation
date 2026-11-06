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

from interface.cli.detect import main as detector
from interface.cli.rca import main as investigator
from interface.jobs.local import main as scheduler
from interface.jobs.worker import worker_main
from interface.web.__main__ import main as api


def tasks(argv: list[str]) -> int:
    if not argv or argv[0] != "worker":
        print("用法：python3 -m main tasks worker --queue <realtime|alert|batch> [--concurrency N]", file=sys.stderr)
        return 2
    return worker_main(argv[1:])


ROLES = {"detector": detector, "investigator": investigator, "scheduler": scheduler, "tasks": tasks, "api": lambda argv: api()}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ROLES:
        print(f"用法：python3 -m main <{'|'.join(ROLES)}> …", file=sys.stderr)
        return 2
    return int(ROLES[argv[0]](argv[1:]) or 0)


if __name__ == "__main__":
    sys.exit(main())
