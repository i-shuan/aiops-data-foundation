"""test_scheduler 用：python -m tests._run_code "<python 程式碼>"，讓排程器的子行程跑一小段程式（睡、失敗、計數），不連 ES。"""
import sys

exec(sys.argv[1])
