#!/usr/bin/env bash
# 在本機的 k3d cluster obs-test 上實測：正式的收集設定（platform/otel/）加範例程式，
# 數值、log、trace 有沒有進 Elasticsearch、有沒有進對部門。
#
#   ./tests/obs/run.sh                  部署，查分流，再把 demo-api 弄慢看 Elasticsearch 裡看不看得到（約 5 分鐘）
#   ./tests/obs/run.sh outage           停掉 Elasticsearch、中間重啟收集程式、再恢復，比筆數（約 7 分鐘，要先跑過上面那個）
#   ./tests/obs/run.sh restart          讓一個容器重啟，新舊兩個容器的 log 都要收得到（約 2 分鐘，要先跑過第一個）
#   ./tests/obs/run.sh broken nospoof   用改壞的設定跑，測試一定要 FAIL。跑完會換回正確的設定（約 8 分鐘）
#   ./tests/obs/run.sh broken retry0    同上：重試改成 0（約 8 分鐘）
#   ./tests/obs/run.sh broken nopvc     同上：佇列不放 PVC（約 8 分鐘）
#
# 要先有 cluster obs-test，並且裝好 OpenTelemetry Operator，做法見 .agents/project/commands.md。
# 這支腳本不建也不刪 cluster，也不裝 Operator。
# 只對 k3d-obs-test 下指令，context 寫死在這裡，不看目前的 context。
#
# 套用 platform/otel/ 時只換兩個值：Elasticsearch 的位址、cluster 的名稱。其他一字不改。
# 密碼當場隨機產生，放進 Secret es-credentials，不會寫進任何檔案。
set -euo pipefail
export MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/../.."

CTX=k3d-obs-test
NS=otel-collector          # 收集程式住的 namespace，跟 platform/otel/ 裡寫的一樣
ES_NS=elastic             # 測試用的 Elasticsearch 住的 namespace
K="kubectl --context $CTX"
H="helm --kube-context $CTX"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# kubectl 是 Windows 程式的話看不懂 Git Bash 的 /tmp
TMPW=$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")

render() {   # $1 = good | nospoof | retry0 | nopvc
  sed -e 's#ES_ENDPOINT: .*#ES_ENDPOINT: "http://elasticsearch.elastic.svc:9200"#' \
      -e 's#CLUSTER: .*#CLUSTER: "obs-test"#' platform/otel/site-ctsp.yaml > "$TMP/site.yaml"
  for f in cluster-collector node-collector; do
    case "$1" in
      # 不再丟掉樣本自己帶的：把要刪的欄位名稱換成不存在的
      nospoof) sed -e 's/delete_key(datapoint.attributes, "/delete_key(datapoint.attributes, "unused./' \
                   -e 's/delete_key(log.attributes, "/delete_key(log.attributes, "unused./' \
                   -e 's/delete_key(span.attributes, "/delete_key(span.attributes, "unused./' platform/otel/$f.yaml ;;
      retry0)  sed 's/max_retries: 100000/max_retries: 0/' platform/otel/$f.yaml ;;
      *)       cat platform/otel/$f.yaml ;;
    esac > "$TMP/$f.yaml"
  done
  if [ "$1" = nopvc ]; then
    python - "$TMPW/cluster-collector.yaml" <<'PY'
import io, sys, yaml
docs = [d for d in yaml.safe_load_all(io.open(sys.argv[1], encoding="utf-8")) if d]
for d in docs:
    if d["kind"] == "StatefulSet":
        del d["spec"]["volumeClaimTemplates"]
        d["spec"]["template"]["spec"]["volumes"].append({"name": "queue", "emptyDir": {}})
yaml.safe_dump_all(docs, io.open(sys.argv[1], "w", encoding="utf-8"), allow_unicode=True)
PY
  fi
}

deploy() {   # $1 = good | nospoof | retry0 | nopvc
  echo "=== 部署（收集設定：$1）==="
  $K get ns kube-system >/dev/null || { echo "連不上 $CTX。cluster 的建法見 .agents/project/commands.md"; exit 1; }
  $K get crd instrumentations.opentelemetry.io >/dev/null 2>&1 || {
    echo "還沒裝 OpenTelemetry Operator（trace 的自動埋點要用）。安裝指令見 .agents/project/commands.md"; exit 1; }
  $K apply -f platform/otel/common.yaml >/dev/null
  # 自動埋點的設定要比範例程式先到：Operator 是在 pod 啟動的那一刻塞進去的
  $K apply -f platform/otel/instrumentation.yaml >/dev/null
  $K apply -f tests/obs/backends.yaml >/dev/null
  # 同一組帳密要放兩份：Elasticsearch 自己用一份，收集程式用一份。Secret 不能跨 namespace 讀
  if ! $K -n $NS get secret es-credentials >/dev/null 2>&1 || ! $K -n $ES_NS get secret es-credentials >/dev/null 2>&1; then
    pw="$(python -c 'import secrets;print(secrets.token_urlsafe(24))' | tr -d '\r')"
    for ns in $NS $ES_NS; do
      $K -n $ns delete secret es-credentials --ignore-not-found >/dev/null
      $K -n $ns create secret generic es-credentials --from-literal=username=elastic --from-literal=password="$pw" >/dev/null
    done
    unset pw
  fi
  $K -n $ES_NS rollout status statefulset/elasticsearch --timeout=300s | tail -1

  if [ "${SKIP_BUILD:-0}" != 1 ]; then
    docker build -q --build-arg BASE_IMAGE=python:3.12-slim -t demo-svc:test app/demo-svc >/dev/null
    k3d image import demo-svc:test -c obs-test >/dev/null 2>&1
  fi
  $H upgrade --install demo-tsid app/demo-svc/k8s -n default --reset-values --wait >/dev/null
  $H upgrade --install demo-bsid app/demo-svc/k8s -n default --reset-values --wait \
    --set namespace=demo-bsid --set observability.tenant=bsid --set observability.dept=D4100 >/dev/null

  render "$1"
  # StatefulSet 的 volumeClaimTemplates 不能改，有沒有 PVC 跟現在的不一樣就要砍掉重建
  have=$($K -n $NS get statefulset otel-cluster-collector -o jsonpath='{.spec.volumeClaimTemplates[0].metadata.name}' 2>/dev/null || true)
  want=queue
  if [ "$1" = nopvc ]; then want=; fi
  if [ "$have" != "$want" ]; then
    $K -n $NS delete statefulset otel-cluster-collector --ignore-not-found >/dev/null
  fi
  $K apply -f "$TMPW/site.yaml" -f "$TMPW/cluster-collector.yaml" -f "$TMPW/node-collector.yaml" >/dev/null
  # 設定放在 ConfigMap 裡，改了不會自己重啟
  $K -n $NS rollout restart statefulset/otel-cluster-collector daemonset/otel-node-collector >/dev/null
  $K -n $NS rollout status statefulset/otel-cluster-collector --timeout=180s | tail -1
  $K -n $NS rollout status daemonset/otel-node-collector --timeout=180s | tail -1
  for ns in fake-none fake-typo fake-spoof; do
    $K -n $ns rollout status deployment/fake --timeout=120s >/dev/null
  done
}

case "${1:-routing}" in
  routing)
    deploy good
    python tests/obs/check.py routing
    python tests/obs/check.py fault
    ;;
  outage)
    python tests/obs/check.py outage
    ;;
  restart)
    python tests/obs/check.py restart
    ;;
  broken)
    case "${2:-}" in
      nospoof) check=routing ;;
      retry0|nopvc) check=outage ;;
      *) echo "用法：$0 broken nospoof|retry0|nopvc"; exit 2 ;;
    esac
    deploy "$2"
    # 不等的話查到的還是改壞之前收的資料，測試會誤判成通過
    echo "等 150 秒，讓最近的資料都是改壞的設定收的"
    sleep 150
    rc=0
    if python tests/obs/check.py "$check"; then
      echo "✘ 設定改壞了（$2），測試卻通過。這個測試抓不到這種錯"
      rc=1
    else
      echo "✔ 改壞的設定（$2）被測試抓到了：上面的 FAIL 是預期的"
    fi
    SKIP_BUILD=1 deploy good
    exit $rc
    ;;
  *)
    sed -n '2,17p' "$0"
    exit 2
    ;;
esac
