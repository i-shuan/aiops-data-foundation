#!/usr/bin/env bash
# 規則（狀態變化、離極限多遠）端到端驗證：detector rules + configs/baseline/rules.yaml。
#
# 在 namespace rules-test 部署 5 個故意壞掉的應用（rules-apps.yaml），等 6 分鐘，跑規則，檢查：
#   - 每一種壞法都被對的規則抓到
#   - 其他正常的應用沒有被抓（除了下面故意調低門檻的節點磁碟）
#   - 節點磁碟：測試機沒辦法真的塞滿，把門檻臨時調成 1%，確認「比例」那條路是通的
#
# 前提：run-e2e.sh、run-metrics-e2e.sh 跑過（測試 ES、Collector 都在）。約 8 分鐘。
# 用法：ES_URL=http://<ip>:9200 e2e/local-k3s/run-rules-e2e.sh      （KEEP=1：跑完不刪測試應用）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }

echo "== 1. API key（baseline 的 key：讀 metrics、寫 aiops-*）"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/telemetry/scripts/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
kubectl -n otel-collector create secret generic es-writer-keys \
  --from-literal=ES_WRITER_API_KEY="$(key_of '寫入 key：es-writer')" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
BASELINE_KEY="$(key_of 'AIOps baseline key')"
kubectl -n otel-collector rollout restart daemonset/otel-node-collector deployment/otel-cluster-collector >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null
kubectl -n otel-collector rollout status deployment/otel-cluster-collector --timeout=180s >/dev/null

echo "== 2. 部署 5 個故意壞掉的應用（rules-test），等 6 分鐘"
kubectl delete namespace rules-test --ignore-not-found --wait=true >/dev/null
kubectl create namespace rules-test >/dev/null
kubectl label namespace rules-test dept=D1110 >/dev/null
kubectl apply -f "${here}/rules-apps.yaml" >/dev/null
sleep 360

echo "== 3. 跑規則（節點磁碟的門檻臨時調成 1%）"
cfg="$(mktemp --suffix=.yaml)"
python3 - "${repo}/engine/configs/baseline/rules.yaml" "${cfg}" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for r in cfg["rules"]:
    if r["name"] == "node.disk_high":
        r["threshold"], r["for"] = 0.01, "1m"
yaml.safe_dump(cfg, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
PY
out="$(cd "${repo}/engine" && ES_URL="${ES_URL}" ES_API_KEY="${BASELINE_KEY}" python3 -m detector rules --config "${cfg}")"
echo "${out}" | sed 's/^/   /'
# 觸發的整理成「規則 對象」一行一個（對象用 namespace 後面那一段：Pod 名稱去掉亂碼、Deployment 名稱、Job 名稱）
fired="$(grep -oE '^  \[[a-z]+\] .*' <<<"${out}" || true)"
has() {  # has <說明> <規則標題> <對象>
  if grep -F -- "$2" <<<"${fired}" | grep -qF -- "$3"; then echo "  PASS  $1"; else echo "  FAIL  $1：沒有「$2 … $3」"; fail=1; fi
}
has "crash-app：容器重啟"              "容器重啟"                  "crash-app"
has "crash-app：可用副本不夠"          "Deployment 可用副本不夠"    "crash-app"
has "bad-image：Pod 一直 Pending"     "Pod 一直排不上去"           "bad-image"
has "bad-image：可用副本不夠"          "Deployment 可用副本不夠"    "bad-image"
has "mem-hog：記憶體快到上限"          "容器記憶體快到上限"          "mem-hog"
has "no-metrics：抓不到 metrics"      "抓不到應用的 metrics"        "no-metrics"
has "fail-job：Job 失敗"              "Job 有 Pod 失敗"            "fail-job"
has "fail-job：Pod 失敗"              "Pod 失敗"                  "fail-job"
has "節點磁碟（門檻 1%）：比例算得出來"   "節點磁碟快滿"               "vm"
others="$(grep -vF 'rules-test' <<<"${fired}" | grep -vF '節點磁碟快滿' || true)"
check "rules-test 以外沒有被抓（正常的應用不誤報）" "${others}" ""
check "mem-hog 以外的容器沒有記憶體告警" "$(grep -F '容器記憶體快到上限' <<<"${fired}" | grep -vcF mem-hog || true)" 0

echo "== 4. 寫進 aiops-anomalies"
n=$(curl -sS -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "${ES_URL}/aiops-anomalies/_count" \
  -d '{"query":{"bool":{"filter":[{"term":{"level":"rule"}},{"term":{"keys.resource.attributes.k8s.namespace.name":"rules-test"}},{"range":{"@timestamp":{"gte":"now-5m"}}}]}}}' | jq .count)
check "rules-test 的觸發都寫進去了" "${n}" "$(grep -cF 'rules-test' <<<"${fired}")"

rm -f "${cfg}"
[[ -z "${KEEP:-}" ]] && kubectl delete namespace rules-test --wait=false >/dev/null
echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
