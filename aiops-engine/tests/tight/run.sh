#!/bin/bash
# 最緊資源測試（CLAUDE.md「測試要在資源最不充裕的條件下做」）。要 root、本機有 PostgreSQL（pg_ctlcluster 16 main）、pgbouncer、cgroup v1。
#   ./run.sh storm      連線池 20 條 / 4 CPU（對照）→ 池子 2 條 / 1 CPU，9 個工作同時開工；再拆開：只 1 CPU、只池子 2 條、池子 4 條
#   ./run.sh mem        容器記憶體 2000 MB（對照）→ 800 MB，train 替身（700MB）放不下
#   ./run.sh journal    本機日誌的磁碟 256 KB、資料庫停 25 秒、重試 0 秒
#   ./run.sh blip       資料庫斷 20 秒、剛好在每日工作開工時：重試 0 秒＋連線逾時 1 秒 → 重試 30 秒（預設）
#   ./run.sh poison     日誌裡一筆永遠補不進去的寫入
#   ./run.sh all
# 沒有 ES：detect／rules／train 用替身（make_configs.py）。結果與解讀見 docs/pdca/meta/2026-11-tight-resource-test.md
set -u
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cleanup() { pgb_stop; mountpoint -q /tmp/aiops-tight-disk 2>/dev/null && umount /tmp/aiops-tight-disk; pg dropdb --if-exists $DB 2>/dev/null; true; }
trap cleanup EXIT                                                   # 不管怎麼結束（包括報錯）都關 PgBouncer、卸載小磁碟、刪測試資料庫
DSN_DIRECT=postgresql:///$DB; DSN_POOL=postgresql://postgres@127.0.0.1:$PGB_PORT/$DB

storm_run() {  # $1 名稱 $2 池子 $3 cpus $4 秒
  pgb_start "$2"; sched "$1" "$W/storm.yaml" "$4" "$DSN_POOL" "$3"; pgb_stop
  echo "== $1（池子 $2 條、CPU $3）"; summ "$1"; }
cmd_storm() {
  setup; python3 "$W/make_configs.py" "$W"; chmod 644 "$W"/*.yaml
  storm_run control 20 0-3 190; storm_run tight 2 0 190
  storm_run cpu1_only 20 0 130; storm_run pool2_only 2 0-3 130; storm_run pool4 4 0-3 130
}
cmd_mem() {
  setup; python3 "$W/make_configs.py" "$W"; chmod 644 "$W"/*.yaml
  for mb in 2000 800; do sched mem_$mb "$W/mem.yaml" 130 "$DSN_DIRECT" 0-3 $mb; echo "== 記憶體 $mb MB"; summ mem_$mb
    echo "   排程器活到最後：$(grep -c '收到停止' "$W/mem_$mb.log")（1 = 是）"; grep "train.*rc=" "$W/mem_$mb.log" | head -3; done
}
cmd_journal() {
  setup; local D=/tmp/aiops-tight-disk; mkdir -p $D; mount -t tmpfs -o size=256k tmpfs $D; chmod 777 $D
  ( cd "$ROOT" && runuser -u postgres -- $(engine_env $DSN_DIRECT AIOPS_JOURNAL_DIR=$D) python3 "$W/journal_writer.py" > "$W/journal.log" 2>&1 & )
  sleep 8; pg_ctlcluster 16 main stop; sleep 25; echo "== 停機中的日誌"; ls -l $D | tail -n +2 | awk '{print $5, $9}'; df -h $D | tail -1
  pg_ctlcluster 16 main start; sleep 33
  echo "== 寫入程式：$(tail -1 "$W/journal.log")"
  echo "   資料庫裡 j*** 實際 $(pg psql -qAt -d $DB -c "select count(*) from aiops_state where id like 'j%'") 筆（回報成功的要比這少，才是「被告知失敗卻事後補進去」）"
  echo "   結束後日誌檔與磁碟："; ls -l $D | tail -n +2 | awk '{print $5, $9}'; df -h $D | tail -1
  grep -v "^[0-9][0-9]:" "$W/journal.log" | head -3; umount $D
}
cmd_blip() {
  setup; python3 "$W/make_configs.py" "$W"; chmod 644 "$W"/*.yaml
  for spec in "retry0 0 1" "retry30 30 10"; do set -- $spec
    ( cd "$ROOT" && runuser -u postgres -- $(engine_env $DSN_DIRECT AIOPS_PG_CONNECT_TIMEOUT=$3) env AIOPS_PG_RETRY_SECONDS=$2 timeout 150 python3 -m main scheduler --config "$W/daily.yaml" --stage 1 > "$W/$1.log" 2>&1 & )
    while s=$(date +%S | sed 's/^0//'); [ "$s" -lt 55 ] || [ "$s" -gt 57 ]; do sleep 0.5; done
    pg_ctlcluster 16 main stop; while s=$(date +%S | sed 's/^0//'); [ "$s" -lt 15 ] || [ "$s" -gt 17 ]; do sleep 0.5; done
    pg_ctlcluster 16 main start; sleep 40
    echo "== 重試 $2 秒、連線逾時 $3 秒（資料庫斷約 20 秒，剛好在每分鐘整點的工作開工前）"; grep -v "收到停止\|^\[scheduler\] 結束\|開始：\|^  " "$W/$1.log" | head -4; sleep 15; done
}
cmd_poison() {
  setup; local D=$W/journal J=""; J=$(cd "$ROOT" && python3 -c "import sys; sys.path.insert(0,'src'); from common.infra.journal import db_key; print(db_key('$DSN_DIRECT', None))")
  ( cd "$ROOT" && python3 "$W/poison.py" plant "$D" "$J" )
  for n in 1 2; do echo "== 全新程式 $n"; ( cd "$ROOT" && runuser -u postgres -- $(engine_env $DSN_DIRECT AIOPS_JOURNAL_DIR=$D) python3 "$W/poison.py" probe ); done
  echo "   壞的那筆還在嗎：$(ls $D | wc -l) 個檔"
}
case "${1:-}" in storm|mem|journal|blip|poison) cmd_$1;; all) cmd_poison; cmd_journal; cmd_blip; cmd_mem; cmd_storm;; *) sed -n 2,12p "$0"; exit 2;; esac
