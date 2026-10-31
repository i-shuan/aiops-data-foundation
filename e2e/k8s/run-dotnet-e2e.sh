#!/usr/bin/env bash
# .NET 應用端到端驗證：真的 ASP.NET Core 8（當作不能改的舊專案）＋ OpenTelemetry .NET 自動埋點 v1.12.0 ＋ PostgreSQL（Npgsql），程式在 dotnet-app/。
#
#   資料：metrics 用 OTLP 推給 Collector、依連線來源 IP 分到部門；HTTP 指標跟 Java 同一條 baseline 序列；log 的 trace_id 對得到 trace
#   正常時：.NET 規則都不報
#   案例：連線池用光（拿了不還）→ db.pool_exhausted_otlp
#         執行緒池飢餓（同步等非同步＋請求變多）→ dotnet8.thread_pool_growth（.NET 會馬上多開執行緒，佇列不太會排）
#         記憶體洩漏（每個請求 64KB）→ dotnet8.gen2_gc_frequent（container.memory_near_limit 只參考：.NET 的 heap 停在容器的 75%，
#           工作集在 90% 上下，不一定觸發），到底時兩種結局擇一：
#           被容器 OOM 砍掉重開 → container.restarted；或活著但一直丟例外 → dotnet8.exceptions_burst（.NET 8 的例外沒分種類）
#
# 前提：run-e2e.sh、run-metrics-e2e.sh 跑過。約 30 分鐘（第一次要下載 .NET SDK image、NuGet 套件）。
# 用法：ES_URL=http://<ip>:9200 e2e/k8s/run-dotnet-e2e.sh      （KEEP=1：跑完不刪；ONLY=pool,starve,leak 只跑某幾個案例）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
fail=0
ns=metrics-cases
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }
ok() { if [[ "$2" == true ]]; then echo "  PASS  $1${3:+（$3）}"; else echo "  FAIL  $1${3:+（$3）}"; fail=1; fi; }
ctl() { kubectl -n "${ns}" exec deploy/dotnet-app -c app -- sh -c "$1"; }
want() { [[ -z "${ONLY:-}" || ",${ONLY}," == *",$1,"* ]]; }

echo "== 1. API key、Collector（含 OTLP metrics）"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/elasticsearch/code/scripts/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
kubectl -n otel-collector create secret generic es-writer-keys \
  --from-literal=ES_WRITER_API_KEY="$(key_of '寫入 key：es-writer')" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
BASELINE_KEY="$(key_of 'AIOps baseline key')"
"${here}/deploy-collector.sh"
kubectl -n otel-collector rollout restart daemonset/otel-node-collector >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null

echo "== 2. .NET 應用（namespace ${ns}，第一次要編譯幾分鐘）"
kubectl delete namespace "${ns}" --ignore-not-found --wait=true >/dev/null
kubectl create namespace "${ns}" >/dev/null
kubectl label namespace "${ns}" dept=D1110 >/dev/null
kubectl -n "${ns}" create configmap dotnet-app-src --from-file="${here}/dotnet-app/app.csproj" --from-file="${here}/dotnet-app/Program.cs" >/dev/null
# 測試環境的對外 HTTPS 經過會重簽憑證的 proxy：NuGet 要信任它的 CA（沒有這個檔就用系統的）
[[ -f /root/.ccr/ca-bundle.crt ]] && kubectl -n "${ns}" create configmap proxy-ca --from-file=ca-bundle.crt=/root/.ccr/ca-bundle.crt >/dev/null
kubectl apply -f "${here}/dotnet-app.yaml" >/dev/null
kubectl -n "${ns}" rollout status deploy/dotnet-app --timeout=900s >/dev/null
echo "   起來了，跑 4 分鐘累積資料"
sleep 240
es -X POST "${ES_URL}/metrics-*,traces-*,k8s-*/_refresh" >/dev/null

echo "== 3. 資料"
q() { es "${ES_URL}/metrics-*.otel-imc/_count" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"resource.attributes.service.name\":\"dotnet-app\"}},$1,{\"range\":{\"@timestamp\":{\"gte\":\"now-3m\"}}}]}}}" | jq .count; }
ok "HTTP 指標用 OTLP 進 IMC（http.server.request.duration）" "$( [[ $(q '{"exists":{"field":"metrics.http.server.request.duration"}}') -gt 0 ]] && echo true)"
ok "GC、執行緒池（.NET 8：process.runtime.dotnet.*）"     "$( [[ $(q '{"exists":{"field":"metrics.process.runtime.dotnet.thread_pool.threads.count"}}') -gt 0 ]] && echo true)"
ok "連線池（Npgsql）"                                     "$( [[ $(q '{"exists":{"field":"metrics.db.client.connections.usage"}}') -gt 0 ]] && echo true)"
ok "部門是依連線來源判斷的（應用沒填，仍是 IMC／D1110）" "$(es "${ES_URL}/metrics-*.otel-imc/_search?size=1" -d '{"query":{"bool":{"filter":[{"term":{"resource.attributes.service.name":"dotnet-app"}},{"exists":{"field":"metrics.http.server.request.duration"}}]}}}' | jq '.hits.hits[0]._source.resource.attributes.dept == "D1110"')"
series="$(cd "${repo}/engine" && ES_URL="${ES_URL}" ES_API_KEY="${BASELINE_KEY}" python3 - <<'PY'
import yaml, pandas as pd
from detector.es import ES, fetch
cfg = yaml.safe_load(open("configs/baseline/series.yaml"))
end = pd.Timestamp.now(tz="UTC").floor("5min"); start = end - pd.Timedelta("10min")
for s in cfg["series"][:3]:
    df = fetch(ES(), s, start, end, "5m").dropna()
    df = df[df["service"] == "dotnet-app"]
    print(s["name"], len(df), df["value"].iloc[-1] if len(df) else None)
PY
)"
echo "${series}" | sed 's/^/        /'
ok "baseline 的 http.* 序列（跟 Java 同一條）算得出 .NET 的值" "$( [[ $(grep -c ' [1-9][0-9]* ' <<<"${series}") -eq 3 ]] && echo true)"
log="$(es "${ES_URL}/k8s-imc-local/_search?size=20&sort=@timestamp:desc" -d '{"query":{"bool":{"filter":[{"term":{"service.name":"dotnet-app"}},{"exists":{"field":"trace.id"}},{"range":{"@timestamp":{"gte":"now-3m","lte":"now-30s"}}}]}}}')"
check ".NET log 拆得出 level、logger、訊息" "$(jq -r '.hits.hits[0]._source | "\(.log.level) \(.log.logger) \(.message | test("^order [0-9]+ handled$"))"' <<<"${log}")" "info Program true"
tid="$(jq -r '.hits.hits[0]._source.trace.id' <<<"${log}")"
ok ".NET log 的 trace_id 對得到 trace" "$( [[ $(es "${ES_URL}/traces-*/_count" -d "{\"query\":{\"term\":{\"trace_id\":\"${tid}\"}}}" | jq .count) -ge 2 ]] && echo true)" "${tid}"

rules() { (cd "${repo}/engine" && ES_URL="${ES_URL}" ES_API_KEY="${BASELINE_KEY}" python3 -m detector rules --config configs/baseline/rules.yaml --only "$1") | grep -E '^  \[' || true; }
DOTNET=dotnet.gen2_gc_frequent,dotnet.out_of_memory,dotnet.thread_pool_starvation,dotnet.thread_pool_growth,dotnet8.gen2_gc_frequent,dotnet8.exceptions_burst,dotnet8.thread_pool_starvation,dotnet8.thread_pool_growth,db.pool_exhausted_otlp
expect() { if grep -F -- "$3" <<<"$2" | grep -qF dotnet-app; then echo "  PASS  $1"; echo "        $(grep -F -- "$3" <<<"$2" | head -1 | sed 's/^ *//')"; else echo "  FAIL  $1：沒有「$3」"; fail=1; fi; }

echo "== 4. 正常時：.NET 規則都不報"
check "沒有觸發" "$(rules "${DOTNET}")" ""

if want pool; then
echo "== 5. 案例：連線池用光（拿了不還），2.5 分鐘"
ctl "touch /ctl/leakconn"
sleep 150
expect "連線池用光" "$(rules db.pool_exhausted_otlp)" "資料庫連線池用光了"
kubectl -n "${ns}" rollout restart deploy/dotnet-app >/dev/null; kubectl -n "${ns}" rollout status deploy/dotnet-app --timeout=900s >/dev/null; sleep 90
fi

if want starve; then
echo "== 6. 案例：執行緒池飢餓（每個請求同步等 2 秒，同時請求變 5 倍），3 分鐘"
ctl "touch /ctl/starve /ctl/burst"
sleep 180
out="$(rules dotnet8.thread_pool_starvation,dotnet8.thread_pool_growth)"
expect "執行緒池飢餓：執行緒暴增" "${out}" "執行緒暴增"
tp="$(es "${ES_URL}/metrics-*.otel-imc/_search?size=0" -d '{"query":{"bool":{"filter":[{"term":{"resource.attributes.service.name":"dotnet-app"}},{"range":{"@timestamp":{"gte":"now-2m"}}}]}},
  "aggs":{"q":{"max":{"field":"metrics.process.runtime.dotnet.thread_pool.queue.length"}},"t":{"max":{"field":"metrics.process.runtime.dotnet.thread_pool.threads.count"}},
  "c":{"avg":{"field":"metrics.process.cpu.count"}}}}')"
echo "        排隊最多 $(jq '.aggregations.q.value' <<<"${tp}") 個、執行緒 $(jq '.aggregations.t.value' <<<"${tp}") 條"
kubectl -n "${ns}" rollout restart deploy/dotnet-app >/dev/null; kubectl -n "${ns}" rollout status deploy/dotnet-app --timeout=900s >/dev/null; sleep 90
fi

if want leak; then
echo "== 7. 案例：記憶體洩漏（每個請求 64KB 放進不會清的 list；容器記憶體上限 512Mi），最多 15 分鐘"
start=$(date +%s)
ctl "echo 64 > /ctl/leak"
seen_gc="" seen_mem="" seen_restart="" seen_oom=""
pick() { grep -F -- "$2" <<<"$1" | grep -F dotnet-app | head -1 | sed 's/^ *//'; }
while (( $(date +%s) - start < 900 )); do
  sleep 60
  out="$(rules dotnet8.gen2_gc_frequent,container.memory_near_limit,container.restarted,dotnet8.exceptions_burst)"
  m=$(( ($(date +%s) - start) / 60 ))
  [[ -z "${seen_gc}" && -n "$(pick "${out}" Gen2)" ]] && seen_gc="${m} 分鐘：$(pick "${out}" Gen2)"
  [[ -z "${seen_mem}" && -n "$(pick "${out}" 記憶體快到上限)" ]] && seen_mem="${m} 分鐘：$(pick "${out}" 記憶體快到上限)"
  [[ -z "${seen_restart}" && -n "$(pick "${out}" 容器重啟)" ]] && seen_restart="${m} 分鐘：$(pick "${out}" 容器重啟)"
  [[ -z "${seen_oom}" && -n "$(pick "${out}" 例外暴增)" ]] && seen_oom="${m} 分鐘：$(pick "${out}" 例外暴增)"
  echo "   第 ${m} 分鐘：Gen2 ${seen_gc:+✓} 記憶體 ${seen_mem:+✓} 重啟 ${seen_restart:+✓} 例外暴增 ${seen_oom:+✓}"
  [[ -n "${seen_gc}" && ( -n "${seen_restart}" || -n "${seen_oom}" ) ]] && break
done
[[ -n "${seen_gc}" ]] && echo "  PASS  洩漏：Gen2 GC 越來越頻繁（第 ${seen_gc}）" || { echo "  FAIL  洩漏：Gen2 規則沒觸發"; fail=1; }
echo "        （參考）容器記憶體快到上限：${seen_mem:-沒觸發（.NET heap 停在容器的 75%，工作集不一定到 90%）}"
if [[ -n "${seen_restart}" ]]; then echo "  PASS  洩漏到底：被容器 OOM 砍掉、K8s 重開（第 ${seen_restart}）"
elif [[ -n "${seen_oom}" ]]; then echo "  PASS  洩漏到底：程式沒結束，一直丟例外（第 ${seen_oom}）"
else echo "  FAIL  洩漏到底：沒看到重開，也沒看到例外暴增"; fail=1; fi
fi

[[ -z "${KEEP:-}" ]] && kubectl delete namespace "${ns}" --wait=false >/dev/null
echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
