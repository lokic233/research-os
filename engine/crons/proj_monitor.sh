#!/bin/bash
# v3 PROJ-MONITOR cron (5 min) — per-project lanes. DETERMINISTIC. Runs `ros lanes` (the BUG-61 brain):
#   FORWARD  = researcher finished candidate evidence -> the cron forwards it (ros queue submit), observe-only
#   RESEED?  = no live researcher + un-experimented open work -> notify orchestrator (NEVER auto-respawn)
#   ADVANCE? = verdict recorded (yellow) -> notify orchestrator (targeted follow-up OR converge; judgment)
#   AWAIT/HOLD = silent no-op (heartbeat; no make-work, no commit churn)
# Replaces the N self-spawning sub-monitor SESSIONS with ONE deterministic cron over all lanes.
source "$(dirname "$0")/_common.sh"
rc=0
lanes_out="$(ROS lanes 2>&1)"; lanes_rc=$?
# de-dup directory: one marker per (lane,action) so a lane stuck in RESEED?/ADVANCE? notifies ONCE, not
# every 5-min cycle (avoids orchestrator-inbox churn). Markers for lanes no longer in that state are cleared.
NDIR="$CRON_DIR/.proj_notified"; mkdir -p "$NDIR"
echo "$lanes_out" | grep -E '📤|⚠️|🔬' | while IFS= read -r line; do
  pid="$(echo "$line" | grep -oE 'PROJ-[0-9]+' | head -1)"
  case "$line" in
    *FORWARD*)
      # observe + forward only: extract the claim and queue-submit it (the cron NEVER judges).
      # FORWARD self-clears (queued -> lane goes AWAIT), so no sentinel needed; queue dedup blocks dups.
      claim="$(echo "$line" | grep -oE 'CLAIM-[0-9]+' | head -1)"
      if [ -n "$claim" ]; then
        if ROS queue submit --claim "$claim" --project "$pid" --by proj-monitor >/dev/null 2>&1; then
          ROS notify --to orchestrator --event FORWARD --subject "$claim" \
            --detail "$pid: forwarded $claim to committee queue" --by proj-monitor --role proj_monitor --info >/dev/null
          log "FORWARD $claim ($pid)"
        fi
      fi ;;
    *RESEED?*)
      if [ ! -f "$NDIR/$pid.reseed" ]; then
        ROS notify --to orchestrator --event RESEED --subject "$pid" \
          --detail "$(echo "$line" | sed 's/^[[:space:]]*//')" --by proj-monitor --role proj_monitor >/dev/null \
          && touch "$NDIR/$pid.reseed"; log "RESEED? $pid -> orchestrator"
      fi
      rm -f "$NDIR/$pid.advance" ;;
    *ADVANCE?*)
      if [ ! -f "$NDIR/$pid.advance" ]; then
        ROS notify --to orchestrator --event ADVANCE --subject "$pid" \
          --detail "$(echo "$line" | sed 's/^[[:space:]]*//')" --by proj-monitor --role proj_monitor >/dev/null \
          && touch "$NDIR/$pid.advance"; log "ADVANCE? $pid -> orchestrator"
      fi
      rm -f "$NDIR/$pid.reseed" ;;
  esac
done
# clear stale sentinels for lanes that are no longer RESEED?/ADVANCE? (state changed -> allow re-notify later)
for mk in "$NDIR"/*.reseed "$NDIR"/*.advance; do
  [ -e "$mk" ] || continue
  p="$(basename "$mk" | sed -E 's/\.(reseed|advance)$//')"
  act="$(echo "$mk" | grep -oE '(reseed|advance)$')"
  kw="RESEED?"; [ "$act" = "advance" ] && kw="ADVANCE?"
  echo "$lanes_out" | grep -E "$p:" | grep -qF "$kw" || rm -f "$mk"
done
# lanes exit 3 = actionable (handled above), exit 0 = all HOLD/AWAIT (silent healthy). Both are success.
[ "$lanes_rc" -ne 0 ] && [ "$lanes_rc" -ne 3 ] && rc=1
[ "$rc" -eq 0 ] && stamp_alive proj_monitor
exit "$rc"
