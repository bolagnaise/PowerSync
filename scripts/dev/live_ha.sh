#!/usr/bin/env bash
# Dev-only: push this checkout's integration to a live Home Assistant over the
# SSH add-on, keep a backup, restart Core, and read EV decision logs.
#
#   PS_LIVE_HOST=root@<ha-ip> scripts/dev/live_ha.sh deploy [--force]
#   PS_LIVE_HOST=root@<ha-ip> scripts/dev/live_ha.sh rollback
#   PS_LIVE_HOST=root@<ha-ip> scripts/dev/live_ha.sh ev-log [lines]
#   PS_LIVE_HOST=root@<ha-ip> scripts/dev/live_ha.sh status
#
# Never used by releases; HACS remains the release path.
set -euo pipefail

HOST="${PS_LIVE_HOST:?set PS_LIVE_HOST=root@<home-assistant-host>}"
SSH=(ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST")
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
# PS_SRC_ROOT: deploy a snapshot directory containing power_sync/ instead.
SRC_ROOT="${PS_SRC_ROOT:-$REPO/custom_components}"
SRC="$SRC_ROOT/power_sync"
DEST=/config/custom_components/power_sync
BACKUPS=/config/power_sync_backups/dev-deploy
KEEP=5

local_version() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$SRC/manifest.json"; }
remote_version() { "${SSH[@]}" "grep -o '\"version\": *\"[^\"]*\"' $DEST/manifest.json | cut -d'\"' -f4" 2>/dev/null || true; }
# Lines since the latest Core boot (HA logs this loader warning once per boot).
boot_logs() { core_logs 30000 | awk '/We found a custom integration power_sync/{n=0} {buf[++n]=$0} END{for(i=1;i<=n;i++)print buf[i]}'; }
core_logs() { "${SSH[@]}" "curl -s -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" 'http://supervisor/core/logs?lines=${1:-20000}'" | sed 's/\x1b\[[0-9;]*m//g'; }

restart_core() {
  echo "Restarting Home Assistant Core..."
  "${SSH[@]}" "ha core restart" >/dev/null
  for _ in $(seq 1 60); do
    sleep 5
    if "${SSH[@]}" "ha core info --raw-json" 2>/dev/null | grep -q '"result": *"ok"'; then
      echo "Core is back."
      return 0
    fi
  done
  echo "Core did not report healthy within 5 minutes" >&2
  return 1
}

startup_check() {
  sleep 60
  echo "--- power_sync errors since restart ---"
  boot_logs | grep -E 'ERROR.*power_sync|Error setting up entry.*[Pp]ower[Ss]ync|Traceback' | tail -20 || echo "(none)"
}

cmd="${1:-}"
case "$cmd" in
  deploy)
    lv="$(local_version)"; rv="$(remote_version)"
    echo "local $lv  ->  live $rv"
    if [[ "${2:-}" != "--force" && -n "$rv" ]] && [[ "$(printf '%s\n%s\n' "$lv" "$rv" | sort -V | tail -1)" != "$lv" ]]; then
      echo "Live instance runs a newer version ($rv > $lv). Re-run with --force to overwrite." >&2
      exit 2
    fi
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    "${SSH[@]}" "mkdir -p $BACKUPS && tar -C /config/custom_components -cf $BACKUPS/power_sync-$rv-$ts.tar power_sync \
      && ls -1t $BACKUPS/power_sync-*.tar | tail -n +$((KEEP + 1)) | xargs -r rm -f"
    echo "Backed up live copy to $BACKUPS/power_sync-$rv-$ts.tar"
    COPYFILE_DISABLE=1 tar -C "$SRC_ROOT" --exclude '__pycache__' --exclude '*.pyc' -czf - power_sync \
      | "${SSH[@]}" "rm -rf /config/custom_components/.power_sync_staging && mkdir /config/custom_components/.power_sync_staging \
          && tar -C /config/custom_components/.power_sync_staging -xzf - \
          && rm -rf $DEST.old && mv $DEST $DEST.old && mv /config/custom_components/.power_sync_staging/power_sync $DEST \
          && rm -rf $DEST.old /config/custom_components/.power_sync_staging"
    echo "Deployed $lv ($(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet || echo '+dirty'))"
    restart_core
    startup_check
    ;;
  rollback)
    latest="$("${SSH[@]}" "ls -1t $BACKUPS/power_sync-*.tar 2>/dev/null | head -1")"
    [[ -n "$latest" ]] || { echo "No dev-deploy backup found" >&2; exit 1; }
    echo "Restoring $latest"
    "${SSH[@]}" "rm -rf $DEST.old && mv $DEST $DEST.old && tar -C /config/custom_components -xf $latest && rm -rf $DEST.old"
    restart_core
    startup_check
    ;;
  ev-log)
    boot_logs | grep -E 'EV decision:|Auto-schedule: (Failed|Started|Error)|Dynamic EV.*(blocked|refus|Failed)|Solar surplus EV|start_ev_charging_dynamic.*False' \
      | tail -n "${2:-60}"
    ;;
  status)
    echo "live version: $(remote_version)"
    "${SSH[@]}" "ls -1t $BACKUPS 2>/dev/null | head -$KEEP"
    ;;
  *)
    sed -n '2,10p' "$0"; exit 1 ;;
esac
