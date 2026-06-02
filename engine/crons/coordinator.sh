#!/bin/bash
# v3 COORDINATOR cron (2 min) — GPU task queue dispatch. DETERMINISTIC (no judgment). Wraps the hardened
# GPU primitives (BUG-53/54/55: lease double-book refusal, fault auto-release, host_mem_floor gate). For
# each committee-approved, GPU-ready exp with a FREE node + host_mem_floor satisfied -> ros exp dispatch.
# Re-dispatches faulted exps. Notifies ORCHESTRATOR on dispatch/fault; SILENT if no GPU work.
source "$(dirname "$0")/_common.sh"
rc=0; did_something=0
# self-feed discovery: approved GPU-ready exps not yet on the task channel (science gate UNCHANGED)
pend="$(ROS gpu-pending 2>/dev/null)"
# the engine owns node-free + host_mem_floor + committee-approval checks inside `ros exp dispatch`.
# Walk pending exps; let the engine REFUSE anything unsafe (already-leased node, floor, unapproved).
echo "$pend" | grep -oE 'EXP-[0-9]+' | sort -u | while read -r exp; do
  # try each GPU node; dispatch refuses if node busy/leased or floor unmet (BUG-54). --approve respects gate.
  for node in $(ROS gpu status 2>/dev/null | grep -oE 'devgpu[0-9]+' | sort -u); do
    if dout="$(ROS exp dispatch --exp "$exp" --node "$node" --by coordinator 2>&1)"; then
      ROS notify --to orchestrator --event GPU_DISPATCH --subject "$exp" \
        --detail "dispatched $exp -> $node" --by coordinator --role coordinator --info >/dev/null
      log "dispatched $exp -> $node"; break
    fi
  done
done
# fault recovery: any faulted lease -> the engine's --fault path already auto-released; surface to
# orchestrator ONCE per fault-set (re-fires only if the set of faulted results changes — no 2-min churn).
faults="$(ROS gpu-result list 2>/dev/null | grep -i 'FAULT' || true)"
NDIR="$CRON_DIR/.coord_notified"; mkdir -p "$NDIR"; mk="$NDIR/gpu_fault"
if [ -n "$faults" ]; then
  fset="$(echo "$faults" | grep -oE 'GR-[0-9]+' | sort | tr '\n' ';')"
  if [ ! -f "$mk" ] || [ "$(cat "$mk")" != "$fset" ]; then
    ROS notify --to orchestrator --event GPU_FAULT --subject "gpu" \
      --detail "$(echo "$faults" | head -4 | tr '\n' ';')" --by coordinator --role coordinator >/dev/null || rc=1
    printf '%s' "$fset" > "$mk"; log "GPU_FAULT surfaced to orchestrator"
  fi
else
  rm -f "$mk"   # no faults -> clear so a future fault re-surfaces
fi
# success (ran cleanly) -> stamp .alive even if there was no GPU work (silent is healthy).
[ "$rc" -eq 0 ] && stamp_alive coordinator
exit "$rc"
