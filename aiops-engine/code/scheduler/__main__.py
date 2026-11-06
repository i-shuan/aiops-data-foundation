"""相容入口：python3 -m scheduler …（helm 的 args: ["scheduler", …]） 轉給 aiops.interface.jobs.local。"""
import sys

from aiops.interface.jobs.local import main

if __name__ == "__main__":
    sys.exit(main())
