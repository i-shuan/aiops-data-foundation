"""測試排程器用的假工作：python3 -m aiops.interface.jobs.sleep <秒> [exit code]。睡幾秒再以指定的 exit code 結束。
e2e（run-schedule-e2e.sh）用它製造「上一次還在跑」「逾時被砍」「一直失敗」；正式的排程表不會用到。"""
import sys
import time

time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 0)
sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
