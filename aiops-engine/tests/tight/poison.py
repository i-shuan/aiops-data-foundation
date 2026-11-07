"""日誌裡一筆永遠補不進去的寫入（例如升級後表不見了）：之後每個新程式的第一筆寫入、看守的補寫都失敗。
用法：poison.py plant <日誌資料夾> <資料庫代號>   放一筆壞的（假裝是已經不在的程式留下的）
      poison.py probe                            當成一個新程式：寫兩筆、再補寫一次"""
import json
import os
import sys

sys.path[:0] = ["src", "."]
if sys.argv[1] == "plant":
    line = {"at": "2026-11-07T10:00:00+08:00", "sql": "UPDATE aiops_gone_after_upgrade SET doc = doc || %s::jsonb WHERE id = %s", "params": ['{"a":1}', "x"], "many": False}
    import socket
    p = os.path.join(sys.argv[2], f"{sys.argv[3]}-{socket.gethostname()}-99999999-1794000000000.jsonl")
    open(p, "w").write(json.dumps(line, ensure_ascii=False) + "\n")
    os.chmod(p, 0o666)
    print("放了一筆壞的：", os.path.basename(p))
    sys.exit(0)
from common.infra.pg import DB
from common.infra.repos import Repos
from common.scenario import load_scenario

db = DB()
repo = Repos(None, load_scenario(), db=db)


def attempt(label, fn):
    try:
        fn()
        print(f"   {label}: ok")
    except Exception as e:  # noqa: BLE001
        print(f"   {label}: 失敗（{type(e).__name__}: {str(e).splitlines()[0][:70]}）")


attempt("新程式的第一筆正常寫入", lambda: repo.state.set("new-process", {"v": 1}))
attempt("再一筆", lambda: repo.state.set("new-process-2", {"v": 2}))
attempt("看守的補寫", db.recover_journal)
