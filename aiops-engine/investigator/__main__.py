"""相容入口：python3 -m investigator … 轉給 aiops.interface.cli.rca。"""
import sys

from aiops.interface.cli.rca import main

if __name__ == "__main__":
    sys.exit(main())
