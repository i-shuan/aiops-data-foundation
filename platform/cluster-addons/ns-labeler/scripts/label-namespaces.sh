#!/bin/sh
# 依對照表把 dept / group 標到 namespace 上。
#
# 這支是「對帳」不是「一次性腳本」：重跑不會有副作用，已經正確的不會被動到。
# 對不到歸屬的 namespace 故意維持沒有 label，讓它落到 unassigned，
# 再由告警抓出來（見 docs/attribution.md）。
set -eu

REGISTRY=${REGISTRY:-/registry}
CLUSTERS=${CLUSTERS:-"central zk el"}
ENVS=${ENVS:-"prod staging dev"}
DRY_RUN=${DRY_RUN:-false}

lookup() {  # lookup <檔案> <key>
  awk -v k="$2" '$1 !~ /^#/ && $1 == k { print $2; exit }' "$1"
}

# namespace 名稱 → app 段
resolve_app() {
  ns=$1
  for c in $CLUSTERS; do
    case "$ns" in "$c"-*) ns=${ns#"$c"-}; break ;; esac
  done
  for e in $ENVS; do
    case "$ns" in *-"$e") ns=${ns%"-$e"}; break ;; esac
  done
  printf '%s' "$ns"
}

matched=0
changed=0
unmatched=""

for ns in $(kubectl get ns -o jsonpath='{.items[*].metadata.name}'); do
  dept=$(lookup "$REGISTRY/namespaces.tsv" "$ns")
  [ -n "$dept" ] || dept=$(lookup "$REGISTRY/apps.tsv" "$(resolve_app "$ns")")

  if [ -z "$dept" ]; then
    unmatched="$unmatched $ns"
    continue
  fi

  group=$(lookup "$REGISTRY/groups.tsv" "$dept")
  if [ -z "$group" ]; then
    echo "ERROR  $ns 的部門 $dept 在 groups.tsv 找不到 group，跳過不標"
    continue
  fi

  cur_dept=$(kubectl get ns "$ns" -o jsonpath='{.metadata.labels.dept}' 2>/dev/null || true)
  cur_group=$(kubectl get ns "$ns" -o jsonpath='{.metadata.labels.group}' 2>/dev/null || true)

  if [ "$cur_dept" = "$dept" ] && [ "$cur_group" = "$group" ]; then
    matched=$((matched + 1))
    continue
  fi

  echo "LABEL  $ns  dept=$dept group=$group  (原本 dept=${cur_dept:-無} group=${cur_group:-無})"
  [ "$DRY_RUN" = "true" ] || kubectl label ns "$ns" "dept=$dept" "group=$group" --overwrite >/dev/null
  changed=$((changed + 1))
done

echo "---"
echo "已正確 $matched 個，本次調整 $changed 個"
if [ -n "$unmatched" ]; then
  echo "對不到歸屬：$unmatched"
  echo "這些會落到 unassigned。要嘛在 apps.tsv 補一行，要嘛在 namespaces.tsv 標成例外。"
fi
