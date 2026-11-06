"""相容入口：python3 -m detector …（排程表、helm、文件都在用） 轉給 aiops.interface.cli.detect。"""
import sys

from aiops.interface.cli.detect import main

if __name__ == "__main__":
    sys.exit(main())
