"""相容入口：python3 -m tasks worker --queue <realtime|alert|batch> [--concurrency N]（helm 的 args: ["tasks", "worker", …]）
轉給 aiops.interface.jobs.worker。"""
import sys

from aiops.interface.jobs.worker import worker_main


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "worker":
        print("用法：python3 -m tasks worker --queue <realtime|alert|batch> [--concurrency N]", file=sys.stderr)
        return 2
    return worker_main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
