#!/bin/bash
# 最緊資源測試的共用函式（要 root：runuser postgres、cgroup v1、mount tmpfs、pg_ctlcluster）。被 run.sh 引用，不單獨跑。
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)          # aiops-engine/
W=${TIGHT_DIR:-/tmp/aiops-tight}                                  # 工作資料夾（設定檔、紀錄）
DB=aiops_tight                                                    # 測試用資料庫（每次重建）
PGB_PORT=6500
pg() { runuser -u postgres -- "$@"; }

setup() {   # 全新資料庫＋建表（detector db init）；工作資料夾
  mkdir -p "$W" "$W/pgb" "$W/journal"; chmod 777 "$W" "$W/pgb" "$W/journal"
  pg dropdb --if-exists $DB; pg createdb $DB
  (cd "$ROOT" && pg env HOME=/tmp AIOPS_PG_DSN=postgresql:///$DB python3 -m main detector db init >/dev/null)
  cp "$ROOT"/tests/tight/*.py "$W"/ && chmod 644 "$W"/*.py
}

# engine 的環境變數（$1 = DSN；其餘 KEY=VAL 附加）。重試預設 0 秒：最緊
engine_env() { local dsn=$1; shift; echo env HOME=/tmp AIOPS_PG_DSN=$dsn AIOPS_PG_RETRY_SECONDS=${RETRY:-0} "$@"; }

pgb_start() {   # $1 池子大小（session 模式，跟工作鎖相容的唯一模式）  $2 query_wait_timeout（預設 120）
  pgb_stop; pkill -f "pgbouncer -d $W/pgb/pgb.ini" 2>/dev/null; sleep 0.5      # 上一次沒收乾淨的先關掉，不然對照組會接在舊的上面
  cat > "$W/pgb/pgb.ini" <<INI
[databases]
$DB = host=/var/run/postgresql dbname=$DB user=postgres
[pgbouncer]
listen_addr = 127.0.0.1
listen_port = $PGB_PORT
auth_type = trust
auth_file = $W/pgb/users.txt
pool_mode = session
default_pool_size = $1
max_db_connections = $1
query_wait_timeout = ${2:-120}
server_reset_query = DISCARD ALL
unix_socket_dir =
logfile = $W/pgb/pgb.log
pidfile = $W/pgb/pgb.pid
INI
  echo '"postgres" ""' > "$W/pgb/users.txt"; chown -R postgres "$W/pgb"
  pg pgbouncer -d "$W/pgb/pgb.ini"; sleep 1
}
pgb_stop() { kill "$(cat "$W/pgb/pgb.pid" 2>/dev/null)" 2>/dev/null; true; }

# 排程器跑 $2 秒（cpus = taskset 的 CPU 清單；mem_mb 非空就放進記憶體上限的 cgroup v1）
sched() {  # $1 名稱 $2 設定檔 $3 秒 $4 DSN $5 cpus $6 mem_mb(可空)
  local cmd=(timeout "$3" python3 -m main scheduler --config "$2" --stage 1)
  local run=(taskset -c "$5" runuser -u postgres -- $(engine_env "$4") "${cmd[@]}")
  if [ -n "${6:-}" ]; then
    local C=/sys/fs/cgroup/memory/tight_$1; mkdir -p $C
    echo "${6}M" > $C/memory.limit_in_bytes; echo "${6}M" > $C/memory.memsw.limit_in_bytes
    ( cd "$ROOT" && bash -c "echo \$\$ > $C/cgroup.procs; exec \"\$@\"" _ "${run[@]}" ) > "$W/$1.log" 2>&1
    echo "   記憶體上限 ${6} MB：碰到上限 $(cat $C/memory.failcnt) 次，最高用量 $(( $(cat $C/memory.max_usage_in_bytes) / 1048576 )) MB"
  else
    ( cd "$ROOT" && "${run[@]}" ) > "$W/$1.log" 2>&1
  fi
}
summ() { python3 "$W/summ.py" "$W/$1.log"; }
