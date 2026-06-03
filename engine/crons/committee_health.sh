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
INST="${ROS_INSTANCE}"
shopt -s nullglob 2>/dev/null || true
# ★ BUG-79: scan BOTH committee layouts. v2 staged committees in runtime/committee_run_<CLAIM>-*; the v3
# orchestrator stages them under experiments/<date>/<EXP>/committeeN/. The cron previously scanned ONLY
# the runtime path, so on v3 it ran (stamped .alive = looked healthy) but saw ZERO committees and NEVER
# fired COMMITTEE_READY — the gate was silently blind for the entire cutover. Now we scan both; for the
# experiments-path dirs the dir name carries the EXP, so resolve the CLAIM via the experiment.yaml.
for d in "$RD"/committee_run_* "$INST"/experiments/*/*/committee* "$INST"/experiments/*/*/*/committee*; do
  [ -d "$d" ] || continue
  st="$d/_status.txt"; [ -f "$st" ] || continue
  status="$(cat "$st" 2>/dev/null)"
  name="$(basename "$d")"
  # claim id: prefer one in the dir/path name (v2 runtime style); else resolve via the EXP's experiment.yaml (v3 style)
  claim="$(echo "$d" | grep -oE 'CLAIM-[0-9]+' | head -1)"
  if [ -z "$claim" ]; then
    exp="$(echo "$d" | grep -oE 'EXP-[0-9]+' | head -1)"
    if [ -n "$exp" ]; then
      ef="$(find "$INST/experiments" -path "*/$exp/experiment.yaml" 2>/dev/null | head -1)"
      [ -n "$ef" ] && claim="$(grep -E '^claim_id:' "$ef" 2>/dev/null | head -1 | awk '{print $2}')"
    fi
  fi
  [ -n "$claim" ] || continue
  # ★ BUG-79b: skip a committee whose verdict is ALREADY written AFTER the committee finished — else we
  # nag the orchestrator to "tally" a claim it already dispositioned (make-work churn). A verdict file for
  # this claim newer than the committee's _status.txt means this pass is done. (Two-pass safe: a later
  # committee dir with no newer verdict still fires.)
  done_mt=$(date -u -r "$st" +%s 2>/dev/null || stat -f %m "$st" 2>/dev/null || echo 0)
  newest_verdict_mt=0
  # ★ BUG-91: anchor the claim_id match. `grep -rl "claim_id: $claim"` is a SUBSTRING match — for an
  # un-padded id (e.g. CLAIM-3) or once ids overflow the 4-digit pad (CLAIM-10000+), "claim_id: CLAIM-3"
  # also matches "claim_id: CLAIM-37"/"CLAIM-3-..". A foreign claim's newer verdict would then falsely
  # satisfy the BUG-79b staleness suppression and the cron would NEVER fire COMMITTEE_READY for THIS
  # claim -> a genuinely-ready committee stalls silently (same blind-gate class BUG-79 fixed). Anchor to
  # the full field value (line-start + exact token + optional trailing ws/EOL).
  for vf in $(grep -rlE "^claim_id:[[:space:]]+${claim}[[:space:]]*\$" "$INST/registry/verdicts" 2>/dev/null); do
    vmt=$(date -u -r "$vf" +%s 2>/dev/null || stat -f %m "$vf" 2>/dev/null || echo 0)
    [ "$vmt" -gt "$newest_verdict_mt" ] && newest_verdict_mt=$vmt
  done
  if [ "$newest_verdict_mt" -ge "$done_mt" ] && [ "$newest_verdict_mt" -gt 0 ]; then continue; fi
  # already notified? mark with a sentinel so we don't spam every minute (sentinel lives IN the committee dir)
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
          --subject "$claim" --detail "$name ($claim) ALL_COMMITTEE_DONE — orchestrator: tally + ros verdict write" \
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
