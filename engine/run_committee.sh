#!/bin/bash
# run_committee.sh — dispatch a review packet to all committee members, each via its configured backend.
# GENERIC: reads role->backend from the instance config. Bash 3.2 compatible (macOS). Backend CLIs are
# whatever your config names; this knows common CLI shapes (claude/codex/gemini/metacode) + a generic fallback.
#
# Usage: run_committee.sh --instance <ROOT> --packet <FILE> --out <DIR> [--python <py>]
#   --python: interpreter with pyyaml (default: tries python3 then /usr/bin/python3)
set -u
INSTANCE="" PACKET="" OUT="" PYBIN=""
while [ $# -gt 0 ]; do case "$1" in
  --instance) INSTANCE="$2"; shift 2;; --packet) PACKET="$2"; shift 2;; --out) OUT="$2"; shift 2;;
  --python) PYBIN="$2"; shift 2;; *) shift;; esac; done
[ -z "$INSTANCE" ] || [ -z "$PACKET" ] || [ -z "$OUT" ] && { echo "usage: --instance R --packet F --out D [--python PY]"; exit 2; }
# BUG-13B: pick a python that has pyyaml
if [ -z "$PYBIN" ]; then
  for c in python3 /usr/bin/python3 /opt/homebrew/bin/python3; do
    if "$c" -c "import yaml" 2>/dev/null; then PYBIN="$c"; break; fi
  done
fi
[ -z "$PYBIN" ] && { echo "ERROR: no python with pyyaml found (pass --python)"; exit 3; }
PROMPTS="$INSTANCE/.research-os/prompts/committee"
[ -d "$PROMPTS" ] || PROMPTS="${RESEARCH_OS_PROMPTS:-$(dirname "$0")/../prompts}/committee"
mkdir -p "$OUT"

# read members into a temp file (BUG-13A: no mapfile — Bash 3.2 safe)
MEMBERS_FILE="$OUT/.members.txt"
"$PYBIN" - "$INSTANCE/research-os.config.yaml" > "$MEMBERS_FILE" <<'PY'
import sys,yaml
c=yaml.safe_load(open(sys.argv[1])) or {}
for m in (c.get("committee") or {}).get("members",[]):
    print(f"{m.get('role')} {m.get('backend')}")
PY
[ -s "$MEMBERS_FILE" ] || { echo "ERROR: no committee members parsed from config"; exit 4; }

run_one(){ # role backend
  role="$1"; backend="$2"
  sysp="$(cat "$PROMPTS/${role}_v001.md" "$PROMPTS/_committee_common_v001.md" 2>/dev/null)"
  # BUG-16: when running area_chair, feed it the 5 reviewers' finished vote outputs to aggregate.
  reviewer_block=""
  if [ "$role" = "area_chair" ]; then
    reviewer_block="

=== REVIEWER VOTES TO AGGREGATE (read these; do not re-review) ==="
    # BUG-77: aggregate ONLY the CONFIGURED reviewer roles (source of truth = MEMBERS_FILE), NOT a blind
    # *.out glob. A stale .out from a removed/renamed role (config change, or a resubmit into a dirty OUT
    # dir) was silently folded into the chair's packet as a phantom committee vote — the same integrity
    # hole BUG-21/22 closed for the completion gate, left open here. Now the chair sees exactly the real
    # committee, never an orphan .out.
    for rn in $(awk '{print $1}' "$MEMBERS_FILE"); do
      [ "$rn" = "area_chair" ] && continue
      reviewer_block="$reviewer_block

--- $rn ---
$(cat "$OUT/${rn}.out" 2>/dev/null)"
    done
  fi
  full="$sysp

=== REVIEW PACKET ===
$(cat "$PACKET")
=== END PACKET ===$reviewer_block
Output ONLY your structured vote block for role $role."
  case "$backend" in
    claude-*|opus*|sonnet*) claude ${CLAUDE_SANDBOX_FLAG:---dangerously-disable-osx-sandbox} --model "$backend" -p "$full" </dev/null >"$OUT/${role}.out" 2>"$OUT/${role}.err" ;;
    codex*)                 codex --dangerously-disable-osx-sandbox exec --skip-git-repo-check "$full" </dev/null >"$OUT/${role}.out" 2>"$OUT/${role}.err" ;;
    gemini*)                gemini --dangerously-disable-osx-sandbox -p "$full" </dev/null >"$OUT/${role}.out" 2>"$OUT/${role}.err" ;;
    metacode*|avocado*|muse*) metacode --dangerously-disable-osx-sandbox run --yolo "$full" </dev/null >"$OUT/${role}.out" 2>"$OUT/${role}.err" ;;
    *)                      echo "ERROR: unknown backend '$backend' for role $role (no CLI shape match)" >"$OUT/${role}.err"; : >"$OUT/${role}.out" ;;
  esac
  # BUG-12: flag empty/failed output so a parser never miscounts a no-op as a vote
  # BUG-20b: ONE automatic retry on empty output (transient gateway/concurrency empties — esp. claude under load)
  if [ ! -s "$OUT/${role}.out" ]; then
    sleep 5
    case "$backend" in
      claude-*|opus*|sonnet*) claude ${CLAUDE_SANDBOX_FLAG:---dangerously-disable-osx-sandbox} --model "$backend" -p "$full" </dev/null >"$OUT/${role}.out" 2>>"$OUT/${role}.err" ;;
      codex*)                 codex --dangerously-disable-osx-sandbox exec --skip-git-repo-check "$full" </dev/null >"$OUT/${role}.out" 2>>"$OUT/${role}.err" ;;
      gemini*)                gemini --dangerously-disable-osx-sandbox -p "$full" </dev/null >"$OUT/${role}.out" 2>>"$OUT/${role}.err" ;;
      metacode*|avocado*|muse*) metacode --dangerously-disable-osx-sandbox run --yolo "$full" </dev/null >"$OUT/${role}.out" 2>>"$OUT/${role}.err" ;;
    esac
  fi
  if [ ! -s "$OUT/${role}.out" ]; then echo "EMPTY_OUTPUT_NO_VOTE role=$role backend=$backend (after 1 retry)" >>"$OUT/${role}.err"; fi
  echo "  done: $role ($backend)"
}

# BUG-16: reviewers in parallel first, then area_chair LAST (so it can aggregate their finished .out).
# BUG-20b: stagger ~3s so concurrent claude procs don't overwhelm the gateway.
# BUG-21: pre-create .err for every member up front + VERIFY all produced .out before signalling DONE,
#         so a dropped/never-invoked member is ALWAYS caught (never a false ALL_COMMITTEE_DONE).
# BUG-22: read the launch loop on FD 9 (NOT stdin) so a backgrounded CLI child cannot drain the member
#         list and truncate the loop. ALL_ROLES is the FULL configured set, computed independently so the
#         completion gate is never fooled by a short-read loop.
CHAIR_ROLE="" CHAIR_BACKEND=""
ALL_ROLES="$(awk '{print $1}' "$MEMBERS_FILE")"
for role in $ALL_ROLES; do : > "$OUT/${role}.err"; done   # pre-create every .err (BUG-21)
while read -r role backend <&9; do
  [ -z "$role" ] && continue
  if [ "$role" = "area_chair" ]; then CHAIR_ROLE="$role"; CHAIR_BACKEND="$backend"; continue; fi
  run_one "$role" "$backend" </dev/null &
  sleep 3
done 9< "$MEMBERS_FILE"
wait   # all reviewer jobs done + .out flushed
if [ -n "$CHAIR_ROLE" ]; then
  echo "  (reviewers done; running $CHAIR_ROLE to aggregate)"
  run_one "$CHAIR_ROLE" "$CHAIR_BACKEND" </dev/null
fi
# BUG-21: completion is gated on EVERY member having a non-empty .out (else INCOMPLETE + list missing)
MISSING=""
for role in $ALL_ROLES; do
  if [ ! -s "$OUT/${role}.out" ]; then
    MISSING="$MISSING $role"
    grep -q EMPTY_OUTPUT_NO_VOTE "$OUT/${role}.err" 2>/dev/null || \
      echo "EMPTY_OUTPUT_NO_VOTE role=$role (no output produced)" >> "$OUT/${role}.err"
  fi
done
if [ -n "$MISSING" ]; then
  echo "COMMITTEE_INCOMPLETE missing:$MISSING" > "$OUT/_status.txt"
  echo "⚠️ committee INCOMPLETE -> $OUT (missing:$MISSING) — do NOT treat as a full committee"
  exit 5
fi
echo "ALL_COMMITTEE_DONE" > "$OUT/_status.txt"
echo "committee run complete -> $OUT (members: $(wc -l < "$MEMBERS_FILE" | tr -d ' '))"
