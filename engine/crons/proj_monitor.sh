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
echo "$lanes_out" | grep -E '📤|⚠️|🔬|🌱' | while IFS= read -r line; do
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
      rm -f "$NDIR/$pid.advance" "$NDIR/$pid.seed" ;;
    *ADVANCE?*)
      if [ ! -f "$NDIR/$pid.advance" ]; then
        ROS notify --to orchestrator --event ADVANCE --subject "$pid" \
          --detail "$(echo "$line" | sed 's/^[[:space:]]*//')" --by proj-monitor --role proj_monitor >/dev/null \
          && touch "$NDIR/$pid.advance"; log "ADVANCE? $pid -> orchestrator"
      fi
      rm -f "$NDIR/$pid.reseed" "$NDIR/$pid.seed" ;;
    *SEED?*)
      # BUG-73: frontier exhausted (all claims terminal) -> orchestrator designs a new claim OR converges.
      if [ ! -f "$NDIR/$pid.seed" ]; then
        ROS notify --to orchestrator --event SEED --subject "$pid" \
          --detail "$(echo "$line" | sed 's/^[[:space:]]*//')" --by proj-monitor --role proj_monitor >/dev/null \
          && touch "$NDIR/$pid.seed"; log "SEED? $pid -> orchestrator"
      fi
      rm -f "$NDIR/$pid.reseed" "$NDIR/$pid.advance" ;;
  esac
done
# ★ BUG-81: instance-level DESIGN? (below investing target — no PROJ id, not a per-lane line). Notify the
# orchestrator to design new projects, ONCE per deficit-state (sentinel keyed on the deficit count so a
# changed deficit re-notifies; clears when back at target).
design_line="$(echo "$lanes_out" | grep 'DESIGN?')"
if [ -n "$design_line" ]; then
  deficit="$(echo "$design_line" | grep -oE 'below target by [0-9]+' | grep -oE '[0-9]+' | head -1)"
  mk="$NDIR/design.$deficit"
  if [ ! -f "$mk" ]; then
    ROS notify --to orchestrator --event DESIGN --subject "refill-to-target" \
      --detail "$(echo "$design_line" | sed 's/^[[:space:]]*//')" --by proj-monitor --role proj_monitor >/dev/null \
      && { rm -f "$NDIR"/design.* 2>/dev/null; touch "$mk"; }
    log "DESIGN? (below target by ${deficit:-?}) -> orchestrator"
  fi
else
  rm -f "$NDIR"/design.* 2>/dev/null   # back at target -> clear so a future deficit re-notifies
fi
# clear stale sentinels for lanes that are no longer RESEED?/ADVANCE?/SEED? (state changed -> allow re-notify later)
for mk in "$NDIR"/*.reseed "$NDIR"/*.advance "$NDIR"/*.seed; do
  [ -e "$mk" ] || continue
  p="$(basename "$mk" | sed -E 's/\.(reseed|advance|seed)$//')"
  act="$(echo "$mk" | grep -oE '(reseed|advance|seed)$')"
  case "$act" in reseed) kw="RESEED?";; advance) kw="ADVANCE?";; seed) kw="SEED?";; esac
  echo "$lanes_out" | grep -E "$p:" | grep -qF "$kw" || rm -f "$mk"
done
# lanes exit 3 = actionable (handled above), exit 0 = all HOLD/AWAIT (silent healthy). Both are success.
[ "$lanes_rc" -ne 0 ] && [ "$lanes_rc" -ne 3 ] && rc=1

# ★ BUG-115 (dengcchi directive): RESEARCHER-STALL -> notify the orchestrator to TROUBLESHOOT (distinct
# from RESEED). `ros progress` (monitor #4) flags work-not-landing per claim (local-but-uncommitted /
# committee-done-no-verdict / gpu-run-no-result / committee-queued-not-convened). On a v3 instance the
# orchestrator is the commit mediator + committee convener; if those stalls persist it usually means a
# researcher/lane is wedged. Forward the stall set to the orchestrator (notify-once per stall-set; clears
# when progress is clean) so an unhealthy researcher gets troubleshot, not silently parked.
prog_out="$(ROS progress 2>&1)"; prog_rc=$?
if [ "$prog_rc" -eq 3 ]; then
  stallset="$(echo "$prog_out" | grep '🔴' | grep -oE 'CLAIM-[0-9]+ \([^)]*\) [a-z-]+' | sort | tr '\n' ';')"
  if [ ! -f "$NDIR/researcher_stall" ] || [ "$(cat "$NDIR/researcher_stall" 2>/dev/null)" != "$stallset" ]; then
    ROS notify --to orchestrator --event RESEARCHER_STALL --subject "work-not-landing" \
      --detail "$(echo "$prog_out" | grep '🔴' | head -8 | tr '\n' ';') — troubleshoot the wedged researcher/lane (re-commit / convene committee / fault+re-dispatch / reseed)" \
      --by proj-monitor --role proj_monitor >/dev/null && printf '%s' "$stallset" > "$NDIR/researcher_stall"
    log "RESEARCHER_STALL -> orchestrator ($(echo "$stallset" | tr ';' '\n' | grep -c CLAIM) claim(s))"
  fi
else
  rm -f "$NDIR/researcher_stall"   # progress clean -> clear so a future stall re-notifies
fi

[ "$rc" -eq 0 ] && stamp_alive proj_monitor
exit "$rc"
