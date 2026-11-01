#!/usr/bin/env bash
# 測試用：把 elasticsearch/helmchart（測試用的 ES，預設兩個 Pod）裝進（或更新到）namespace es-test。
# 用法：e2e/k8s/deploy-es.sh [helm 的額外參數…]      例如 --set replicas=1 --set twoNode.enabled=false（單節點）
#   KCTX=<kube context>（預設目前的）
# 兩節點需要節點之間的 TLS 憑證：沒有 Secret es-transport-certs 就先產生。
# 原本是單節點、要升成兩節點時，要分兩步：先讓 elasticsearch-0 改成多節點設定（它自己就是 master）、再加 elasticsearch-1。
# 一次做的話，StatefulSet 會先開 elasticsearch-1，它找不到 master、一直不 ready，elasticsearch-0 就不會更新，卡死。
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
# Windows 的 helm 看不懂 Git Bash 的 /c/... 路徑，先轉成 C:/...
p() { cygpath -m "$1" 2>/dev/null || echo "$1"; }
k() { kubectl ${KCTX:+--context "${KCTX}"} "$@"; }
chart="$(p "${repo}/elasticsearch/helmchart")"
# --reset-values：Helm 4 的 upgrade 預設會沿用上一次的 --set，上次 --set replicas=1 的話這次沒給就還是 1
install() { helm upgrade --install elasticsearch "${chart}" -n es-test --create-namespace --reset-values ${KCTX:+--kube-context "${KCTX}"} "$@" >/dev/null; }

make_certs() {
  local d; d="$(p "$(mktemp -d)")"   # Windows 的 openssl、kubectl 看不懂 /tmp/…
  export MSYS_NO_PATHCONV=1   # Git Bash 會把 -subj /CN=… 當路徑改掉
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=es-test-ca -keyout "${d}/ca.key" -out "${d}/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj /CN=elasticsearch -keyout "${d}/node.key" -out "${d}/node.csr" 2>/dev/null
  openssl x509 -req -in "${d}/node.csr" -CA "${d}/ca.crt" -CAkey "${d}/ca.key" -CAcreateserial -days 3650 -out "${d}/node.crt" 2>/dev/null
  k -n es-test create secret generic es-transport-certs     --from-file="${d}/ca.crt" --from-file="${d}/node.crt" --from-file="${d}/node.key"     --dry-run=client -o yaml | k apply -f - >/dev/null
  rm -rf "${d}"
}

want=2
for a in "$@"; do [[ "$a" == replicas=* ]] && want="${a#replicas=}"; done
k create namespace es-test --dry-run=client -o yaml | k apply -f - >/dev/null
k -n es-test get secret es-transport-certs >/dev/null 2>&1 || make_certs
cur="$(k -n es-test get statefulset elasticsearch -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
# podManagementPolicy 建好之後不能改：舊的 StatefulSet（沒設，是 OrderedReady）刪掉重建，Pod 和 PVC 留著（--cascade=orphan）
if [[ -n "${cur}" && "$(k -n es-test get statefulset elasticsearch -o jsonpath='{.spec.podManagementPolicy}')" != Parallel ]]; then
  k -n es-test delete statefulset elasticsearch --cascade=orphan >/dev/null
fi
if [[ -n "${cur}" && "${cur}" -ge 1 && "${cur}" -lt "${want}" ]]; then
  # 已經有的先用新設定換版（replicas 維持現在的數字），再放大
  install "$@" --set "replicas=${cur}"
  k -n es-test rollout status statefulset/elasticsearch --timeout=600s >/dev/null
fi
install "$@"
