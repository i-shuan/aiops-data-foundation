#!/usr/bin/env bash
# 本機只有一個 cluster（rancher-desktop），e2e 腳本的 namespace 寫死（es-test、otel-collector…），
# 幾個工作同時部署會互相蓋掉。上 cluster 前拿鎖、用完放掉：
#   source e2e/k8s/cluster-lock.sh
#   cluster_lock "phase1-壓測"        # 等到拿到為止（每 30 秒看一次）；印出目前是誰在用
#   ... kubectl／helm／e2e 腳本 ...
#   cluster_unlock
# 鎖是一個資料夾（mkdir 是原子操作）：~/.aiops-cluster.lock，裡面的 owner 記誰、何時拿的。
# 拿鎖的工作掛了、鎖沒放：看 owner 的時間，確定那個工作已經不在跑，再手動 rm -rf ~/.aiops-cluster.lock。
CLUSTER_LOCK_DIR="${CLUSTER_LOCK_DIR:-${HOME}/.aiops-cluster.lock}"

cluster_lock() {
  local who="${1:?要給一個名字，例如 phase1-壓測}"
  until mkdir "${CLUSTER_LOCK_DIR}" 2>/dev/null; do
    echo "$(date +%H:%M:%S) cluster 使用中：$(cat "${CLUSTER_LOCK_DIR}/owner" 2>/dev/null || echo 不明)，30 秒後再試"
    sleep 30
  done
  echo "${who} $(date -u +%Y-%m-%dT%H:%M:%SZ) pid $$" > "${CLUSTER_LOCK_DIR}/owner"
  trap cluster_unlock EXIT
  echo "$(date +%H:%M:%S) 拿到 cluster：${who}"
}

cluster_unlock() {
  [[ -d "${CLUSTER_LOCK_DIR}" ]] && grep -q "pid $$\$" "${CLUSTER_LOCK_DIR}/owner" 2>/dev/null && rm -rf "${CLUSTER_LOCK_DIR}" && echo "$(date +%H:%M:%S) 放掉 cluster"
  trap - EXIT
}
