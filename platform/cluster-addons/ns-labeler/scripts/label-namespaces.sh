#!/bin/sh
# 依對照表把 dept / group / system 標到 namespace 上。
#
# dept   這份資料是誰的，只有一個值。
# group  要送到哪些 tenant。平常由 dept 推出；對照表有填就用填的，可以是 it.imc 這種多值。
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

val() {  # 同 lookup，但把佔位用的 "-" 當成沒填
  v=$(lookup "$1" "$2" "$3")
  [ "$v" = "-" ] && v=""
  printf '%s' "$v"
}

# 已知的 group = registry.groups 裡出現過的值。多值 group 的每一段都要在這裡面。
known_groups=" $(awk '$1 !~ /^#/ && NF >= 2 { print $2 }' "$REGISTRY/groups.tsv" | sort -u | tr '\n' ' ')"

# 檢查 group。不合法時印出原因並回傳 1。
# 用 why=$(check_group ...) 呼叫，所以跑在子 shell 裡，變數不會外洩。
check_group() {
  n=0
  has_platform=0
  for g in $(printf '%s' "$1" | tr '.' ' '); do
    n=$((n + 1))
    case "$known_groups" in *" $g "*) ;; *) echo "不存在的 group「$g」（已知：$known_groups）"; return 1 ;; esac
    [ "$g" != "platform" ] || has_platform=1
  done
  # platform 是兜底：vmagent 的兜底那條用 drop 認「不屬於任何 tenant」，
  # 所以 it.platform 只會進 IT，平台收不到。擋下來，免得有人以為兩邊都有。
  if [ "$n" -gt 1 ] && [ "$has_platform" = 1 ]; then
    echo "platform 是兜底，不能跟別的 group 組合"
    return 1
  fi
  return 0
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
  # namespaces.tsv 優先（例外、平台自己的）。格式：namespace 部門 系統 group，後兩欄可為 "-"
  dept=$(val "$REGISTRY/namespaces.tsv" "$ns" 2)
  system=$(val "$REGISTRY/namespaces.tsv" "$ns" 3)
  share=$(val "$REGISTRY/namespaces.tsv" "$ns" 4)
  if [ -z "$dept" ]; then
    app=$(resolve_app "$ns")
    dept=$(val "$REGISTRY/apps.tsv" "$app" 2)
    system=$(val "$REGISTRY/apps.tsv" "$app" 3)
    share=$(val "$REGISTRY/apps.tsv" "$app" 4)
  fi

  if [ -z "$dept" ]; then
    unmatched="$unmatched $ns"
    continue
  fi

  if [ -n "$share" ]; then
    group=$share
  else
    group=$(val "$REGISTRY/groups.tsv" "$dept" 2)
  fi
  if [ -z "$group" ]; then
    echo "ERROR  $ns 的部門 $dept 在 registry.groups 找不到 group，跳過不標"
    continue
  fi
  if ! why=$(check_group "$group"); then
    echo "ERROR  $ns 的 group=$group 不合法：$why。跳過不標"
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
