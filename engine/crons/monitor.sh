#!/bin/bash
# v3 MONITOR cron (5 min) — system-health watchdog. DETERMINISTIC. Jobs:
#  #1 cron-health: are the other crons alive? (.alive stamps)  -> escalate troubleshooter
#  #4 progress:    is work landing? (per-claim stall taxonomy)  -> notify orchestrator
# Notifies the TROUBLESHOOTER (main navi session) for system-health (dead crons). Notifies the
# ORCHESTRATOR for work-not-landing (#4). Stamps its OWN .alive on success so the main navi session /
# dead-man check can see the monitor itself is alive.
source "$(dirname "$0")/_common.sh"
rc=0
# #1: check the OTHER crons' .alive stamps (exclude self so a just-booted monitor doesn't self-flag;
#     the MAIN NAVI session checks the monitor's own .alive — "who watches the watchers")
if ! out="$(ROS cron-health --exclude monitor 2>&1)"; then
  # some cron dead/stalled -> escalate to troubleshooter (system-health, main navi)
  bad="$(echo "$out" | sed -n 's/.*DEAD\/STALLED.*: //p')"
  ROS notify --to troubleshooter --event CRON_DEAD --subject "${bad:-unknown}" \
    --detail "ros cron-health flagged dead/stalled cron(s): ${bad}" --by monitor --role monitor >/dev/null || rc=1
  log "CRON_DEAD escalated: ${bad}"
fi
# #4: progress / work-not-landing -> notify orchestrator (it is the commit mediator)
if ! pout="$(ROS progress 2>&1)"; then
  stalls="$(echo "$pout" | grep -c '🔴')"
  ROS notify --to orchestrator --event WORK_NOT_LANDING --subject "progress" \
    --detail "$(echo "$pout" | grep '🔴' | head -8 | tr '\n' ';')" --by monitor --role monitor >/dev/null || rc=1
  log "WORK_NOT_LANDING -> orchestrator ($stalls stall(s))"
fi
# #2/#3 (agent stall + wedged-log) are reaper/liveness duties already in the engine — surface coverage gaps:
ROS reap >/dev/null 2>&1 || true
# success = the monitor RAN its checks cleanly (escalations themselves succeeded). Stamp .alive.
[ "$rc" -eq 0 ] && stamp_alive monitor && log "monitor cycle OK (.alive stamped)"
exit "$rc"
