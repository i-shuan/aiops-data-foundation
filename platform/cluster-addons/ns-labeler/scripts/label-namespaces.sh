#!/bin/sh
# 依對照表把 dept / group / system 標到 namespace 上。
#
# 這支是「對帳」不是「一次性腳本」：重跑不會有副作用，已經正確的不會被動到。
# 對不到歸屬的 namespace 故意維持沒有 label，讓它落到 unassigned，
# 再由告警抓出來（見 docs/attribution.md）。
#
# dept / group 是分流依據，缺了就不標。
# system 只是分析用的維度，缺了不擋 dept / group，只在 log 裡提醒一聲。
set -eu

REGISTRY=${REGISTRY:-/registry}
CLUSTERS=${CLUSTERS:-"central zk el"}
ENVS=${ENVS:-"prod staging dev"}
DRY_RUN=${DRY_RUN:-false}

lookup() {  # lookup <檔案> <key> <第幾欄>
  awk -v k="$2" -v c="$3" '$1 !~ /^#/ && $1 == k { print $c; exit }' "$1"
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
  # namespaces.tsv 優先（例外、平台自己的）。格式：namespace 部門 [系統]
  dept=$(lookup "$REGISTRY/namespaces.tsv" "$ns" 2)
  system=$(lookup "$REGISTRY/namespaces.tsv" "$ns" 3)
  if [ -z "$dept" ]; then
    # apps.tsv。格式：app 部門 [系統]
    app=$(resolve_app "$ns")
    dept=$(lookup "$REGISTRY/apps.tsv" "$app" 2)
    system=$(lookup "$REGISTRY/apps.tsv" "$app" 3)
  fi

  if [ -z "$dept" ]; then
    unmatched="$unmatched $ns"
    continue
  fi

  group=$(lookup "$REGISTRY/groups.tsv" "$dept" 2)
  if [ -z "$group" ]; then
    echo "ERROR  $ns 的部門 $dept 在 registry.groups 找不到 group，跳過不標"
    continue
  fi

  [ -n "$system" ] || echo "WARN   $ns 沒有 system，log 上會填 unknown"

  cur_dept=$(kubectl get ns "$ns" -o jsonpath='{.metadata.labels.dept}' 2>/dev/null || true)
  cur_group=$(kubectl get ns "$ns" -o jsonpath='{.metadata.labels.group}' 2>/dev/null || true)
  cur_system=$(kubectl get ns "$ns" -o jsonpath='{.metadata.labels.system}' 2>/dev/null || true)

  if [ "$cur_dept" = "$dept" ] && [ "$cur_group" = "$group" ] \
     && { [ -z "$system" ] || [ "$cur_system" = "$system" ]; }; then
    matched=$((matched + 1))
    continue
  fi

  set -- "dept=$dept" "group=$group"
  [ -z "$system" ] || set -- "$@" "system=$system"

  echo "LABEL  $ns  $*  (原本 dept=${cur_dept:-無} group=${cur_group:-無} system=${cur_system:-無})"
  [ "$DRY_RUN" = "true" ] || kubectl label ns "$ns" "$@" --overwrite >/dev/null
  changed=$((changed + 1))
done

echo "---"
echo "已正確 $matched 個，本次調整 $changed 個"
if [ -n "$unmatched" ]; then
  echo "對不到歸屬：$unmatched"
  echo "這些會落到 unassigned。要嘛在 values.yaml 的 registry.apps 補一行，要嘛在 registry.namespaces 標成例外。"
fi
