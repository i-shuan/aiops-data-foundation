"""相容入口：python3 -m api 轉給 aiops.interface.web.__main__。"""
import sys

from aiops.interface.web.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
