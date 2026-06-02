#!/bin/bash
# v3 COMMITTEE-HEALTH cron (1 min) — the gate. DETERMINISTIC. Polls in-flight committee runs:
#  - _status.txt == ALL_COMMITTEE_DONE AND every member .out non-empty AND no EMPTY_OUTPUT_NO_VOTE
#    -> notify orchestrator "CLAIM-X ready to tally"
#  - COMMITTEE_INCOMPLETE / stalled -> notify orchestrator "INCOMPLETE"
# SILENT if no in-flight runs. Never tallies/judges (that's the orchestrator). Reuses run_committee.sh's
# own gate artifacts (_status.txt, per-role .out/.err) — does NOT re-implement the 6/6 gate.
source "$(dirname "$0")/_common.sh"
rc=0
RD="$(RUNTIME_DIR)"
shopt -s nullglob 2>/dev/null || true
for d in "$RD"/committee_run_*; do
  [ -d "$d" ] || continue
  st="$d/_status.txt"; [ -f "$st" ] || continue
  status="$(cat "$st" 2>/dev/null)"
  name="$(basename "$d")"
  claim="$(echo "$name" | grep -oE 'CLAIM-[0-9]+' | head -1)"
  [ -n "$claim" ] || continue
  # already notified? mark with a sentinel so we don't spam every minute
  notified="$d/.v3_notified"
  case "$status" in
    ALL_COMMITTEE_DONE)
      # verify every member produced a non-empty .out and no EMPTY_OUTPUT_NO_VOTE (defence-in-depth)
      empty="$(grep -l EMPTY_OUTPUT_NO_VOTE "$d"/*.err 2>/dev/null | wc -l | tr -d ' ')"
      if [ "$empty" != "0" ]; then
        [ -f "$notified.incomplete" ] || { ROS notify --to orchestrator --event COMMITTEE_INCOMPLETE \
          --subject "$claim" --detail "$name: EMPTY_OUTPUT_NO_VOTE in $empty member(s)" --by committee-health \
          --role committee_health >/dev/null && touch "$notified.incomplete"; log "INCOMPLETE $claim"; }
      else
        [ -f "$notified.ready" ] || { ROS notify --to orchestrator --event COMMITTEE_READY \
          --subject "$claim" --detail "$name ALL_COMMITTEE_DONE — orchestrator: tally + ros verdict write" \
          --by committee-health --role committee_health >/dev/null && touch "$notified.ready"; log "READY $claim"; }
      fi
      ;;
    COMMITTEE_INCOMPLETE*)
      [ -f "$notified.incomplete" ] || { ROS notify --to orchestrator --event COMMITTEE_INCOMPLETE \
        --subject "$claim" --detail "$name: $status" --by committee-health --role committee_health >/dev/null \
        && touch "$notified.incomplete"; log "INCOMPLETE $claim"; }
      ;;
  esac
done
[ "$rc" -eq 0 ] && stamp_alive committee_health
exit "$rc"
