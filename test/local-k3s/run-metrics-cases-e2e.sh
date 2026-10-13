#!/usr/bin/env bash
# 指標案例端到端驗證（docs/metrics/cases.md）：四個領域各一個案例，資料走真正的路線（Collector → ES），
# 用規則（aiops/baseline/rules.yaml）判斷，檢查「該報的報、不該報的不報」。
#
#   1 基礎架構：CPU 被限流     cpu-burst（limit 200m、突波）                     → container.cpu_throttled
#   2 應用：記憶體洩漏          order-app（真的 Java＋OpenTelemetry agent）/tmp/leak → jvm.old_gen_after_gc_high、jvm.major_gc_frequent、OOM 後 container.restarted
#   3 DB／佇列：連線池用光＋落後  order-app /tmp/leakconn（真的 HikariCP）            → db.pool_exhausted、db.pool_waiting
#                              kafka-exporter-sim /tmp/lag_up                     → kafka.consumer_lag、kafka.consumer_lag_growing
#   4 機台：通訊斷線、PLC 變慢   eap-exporter-sim EQP-07 斷線、EQP-03 掃描 120ms＋重傳 → eap.*
#
# 前提：run-e2e.sh、run-metrics-e2e.sh 跑過。約 25 分鐘。需要能從 Pod 連到 GitHub、Maven Central（下載 agent、HikariCP）。
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/run-metrics-cases-e2e.sh      （KEEP=1：跑完不刪；ONLY=2：只跑案例 2，可寫 ONLY=1,3）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
fail=0
ns=metrics-cases
ts() { date +%H:%M:%S; }
want() { [[ -z "${ONLY:-}" || ",${ONLY}," == *",$1,"* ]]; }
app() { kubectl -n "${ns}" exec "deploy/$1" -c app -- sh -c "$2"; }

echo "== 1. API key、Collector（含 cAdvisor 限流）"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/platform/central/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
kubectl -n otel-collector create secret generic es-writer-keys \
  --from-literal=IT_ES_API_KEY="$(key_of '寫入 key：es-writer-it')" \
  --from-literal=IMC_ES_API_KEY="$(key_of '寫入 key：es-writer-imc')" \
  --from-literal=UNASSIGNED_ES_API_KEY="$(key_of '寫入 key：es-writer-unassigned')" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
BASELINE_KEY="$(key_of 'AIOps baseline key')"
kubectl apply -k "${here}/otel-collector" >/dev/null
kubectl -n otel-collector rollout restart daemonset/otel-node-collector deployment/otel-cluster-collector >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null

echo "== 2. 測試應用（namespace ${ns}）"
kubectl delete namespace "${ns}" --ignore-not-found --wait=true >/dev/null
kubectl create namespace "${ns}" >/dev/null
kubectl label namespace "${ns}" dept=D1110 >/dev/null
kubectl -n "${ns}" create configmap order-app --from-file="${here}/java-app/OrderApp.java" >/dev/null
kubectl apply -f "${here}/java-app.yaml" -f "${here}/cpu-burst.yaml" -f "${here}/exporter-sims.yaml" >/dev/null
for d in order-app cpu-burst kafka-exporter-sim eap-exporter-sim; do kubectl -n "${ns}" rollout status "deploy/${d}" --timeout=600s >/dev/null; done
echo "   $(ts) 都起來了，先正常跑 6 分鐘（限流要 5 分鐘的資料才判斷）"
sleep 360

# rules <規則,…>：跑這幾條，印出觸發的（一行一個）
rules() {
  (cd "${repo}/aiops" && ES_URL="${ES_URL}" ES_API_KEY="${BASELINE_KEY}" python3 -m aiops_baseline rules \
    --config baseline/rules.yaml --only "$1") | grep -E '^  \[' || true
}
expect() {  # expect <說明> <輸出> <規則標題> <對象>
  if grep -F -- "$3" <<<"$2" | grep -qF -- "$4"; then echo "  PASS  $1"; echo "        $(grep -F -- "$3" <<<"$2" | grep -F -- "$4" | head -1 | sed 's/^ *//')"
  else echo "  FAIL  $1：沒有「$3 … $4」"; fail=1; fi
}
none() {  # none <說明> <輸出> <不該出現的字>
  if grep -qF -- "$3" <<<"$2"; then echo "  FAIL  $1：$(grep -F -- "$3" <<<"$2" | head -2)"; fail=1; else echo "  PASS  $1"; fi
}
ALL=container.cpu_throttled,jvm.old_gen_after_gc_high,jvm.major_gc_frequent,jvm.gc_overhead,db.pool_exhausted,db.pool_waiting,kafka.consumer_lag,kafka.consumer_lag_growing,eap.secs_disconnected,eap.plc_scan_slow,eap.secs_retransmits

if want 1; then
echo "== 3. 正常的時候：只有 cpu-burst 被限流（它從一開始就是壞的），其他都不能報"
out="$(rules "${ALL}")"
expect "案例 1 CPU 被限流：cpu-burst" "${out}" "容器 CPU 被限流" "cpu-burst"
cpu="$(curl -sS -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "${ES_URL}/metrics-kubeletstatsreceiver.otel-imc/_search" -d '{"size":0,
  "query":{"bool":{"filter":[{"prefix":{"resource.attributes.k8s.pod.name":"cpu-burst"}},{"exists":{"field":"metrics.container.cpu.usage"}},{"range":{"@timestamp":{"gte":"now-5m"}}}]}},
  "aggs":{"a":{"avg":{"field":"metrics.container.cpu.usage"}}}}' | jq '.aggregations.a.value')"
echo "        cpu-burst 平均 CPU $(python3 -c "print(f'{${cpu}:.3f} 核，limit 的 {${cpu}/0.2:.0%}')")：平均看起來不高，卻一直被限流"
none "order-app 正常：沒有 JVM、連線池告警" "$(grep -v cpu-burst <<<"${out}")" "order-app"
none "Kafka 正常：沒有落後告警" "${out}" "Kafka"
none "機台正常：沒有 EAP 告警" "${out}" "EQP-"
fi

if want 3; then
echo "== 4. 案例 3：連線池用光（真的 HikariCP，拿了不還）＋ Kafka 消費落後，5.5 分鐘"
app order-app "touch /tmp/leakconn"
app kafka-exporter-sim "touch /tmp/lag_up"
sleep 330
out="$(rules db.pool_exhausted,db.pool_waiting,kafka.consumer_lag,kafka.consumer_lag_growing)"
expect "連線池用光：order-app orders-db" "${out}" "資料庫連線池用光了" "orders-db"
expect "有請求排隊等連線"              "${out}" "有請求在排隊等資料庫連線" "orders-db"
expect "Kafka 落後：order-push"       "${out}" "Kafka 消費落後太多" "order-push"
expect "Kafka 落後一直增加：order-push" "${out}" "Kafka 消費落後一直增加" "order-push"
none "Kafka：正常的 report 不報" "${out}" "report"
app kafka-exporter-sim "rm -f /tmp/lag_up"
# 拿走的連線不會自己還，重開應用
kubectl -n "${ns}" rollout restart deploy/order-app >/dev/null
kubectl -n "${ns}" rollout status deploy/order-app --timeout=600s >/dev/null
sleep 90
fi

if want 4; then
echo "== 5. 案例 4：機台（EQP-07 斷線、EQP-03 掃描 120ms＋重傳），5.5 分鐘"
app eap-exporter-sim "touch /tmp/eqp07_down /tmp/eqp03_slow"
sleep 330
out="$(rules eap.secs_disconnected,eap.plc_scan_slow,eap.secs_retransmits)"
expect "機台：EQP-07 通訊斷線"         "${out}" "機台 SECS 通訊斷線" "EQP-07"
expect "機台：EQP-03 PLC 掃描超過規格"  "${out}" "PLC 掃描週期超過規格" "EQP-03"
expect "機台：EQP-03 重傳變多"         "${out}" "SECS 封包重傳變多" "EQP-03"
none "機台：其他 6 台不報" "$(grep -vE 'EQP-0(3|7)' <<<"${out}")" "EQP-"

app eap-exporter-sim "rm -f /tmp/eqp07_down /tmp/eqp03_slow" || true
fi

if want 2; then
echo "== 6. 案例 2：記憶體洩漏（真的 Java，heap 128MB，每個請求 12KB 放進不會清的快取），最多 25 分鐘"
start=$(date +%s)
app order-app "echo 12 > /tmp/leak"
seen_heap="" seen_gc="" seen_restart=""
while (( $(date +%s) - start < 1500 )); do
  sleep 60
  out="$(rules jvm.old_gen_after_gc_high,jvm.major_gc_frequent,container.restarted)"
  m=$(( ($(date +%s) - start) / 60 ))
  [[ -z "${seen_heap}" ]] && grep -q "老年代" <<<"${out}" && seen_heap="${m} 分鐘：$(grep "老年代" <<<"${out}" | head -1 | sed 's/^ *//')"
  [[ -z "${seen_gc}" ]] && grep -q "major GC 太頻繁" <<<"${out}" && seen_gc="${m} 分鐘：$(grep "major GC 太頻繁" <<<"${out}" | head -1 | sed 's/^ *//')"
  [[ -z "${seen_restart}" ]] && grep -q "容器重啟.*order-app" <<<"${out}" && seen_restart="${m} 分鐘：$(grep "容器重啟.*order-app" <<<"${out}" | head -1 | sed 's/^ *//')"
  echo "   $(ts) 第 ${m} 分鐘：老年代 ${seen_heap:+✓} major GC ${seen_gc:+✓} 重啟 ${seen_restart:+✓}"
  [[ -n "${seen_heap}" && -n "${seen_gc}" && -n "${seen_restart}" ]] && break
done
[[ -n "${seen_heap}" ]] && echo "  PASS  洩漏：老年代 GC 後還很滿（第 ${seen_heap}）" || { echo "  FAIL  洩漏：老年代規則沒觸發"; fail=1; }
[[ -n "${seen_gc}" ]] && echo "  PASS  洩漏：major GC 越來越頻繁（第 ${seen_gc}）" || { echo "  FAIL  洩漏：major GC 規則沒觸發"; fail=1; }
[[ -n "${seen_restart}" ]] && echo "  PASS  洩漏到底：OOM 結束、K8s 重開（第 ${seen_restart}）" || { echo "  FAIL  沒看到 OOM 重開"; fail=1; }
fi

[[ -z "${KEEP:-}" ]] && kubectl delete namespace "${ns}" --wait=false >/dev/null
echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
