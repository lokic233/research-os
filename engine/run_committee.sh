#!/bin/bash
# run_committee.sh — dispatch a review packet to all committee members, each via its configured backend.
# GENERIC: reads role->backend mapping + prompts dir from the instance config (research-os.config.yaml).
# Backend CLIs are whatever your config names; this script only knows how to invoke common CLI shapes.
#
# Usage: run_committee.sh --instance <ROOT> --packet <FILE> --out <DIR>
#   config committee.members: [{role, backend}]   (backend e.g. an LLM-CLI model id, configured per instance)
set -u
INSTANCE="" PACKET="" OUT=""
while [ $# -gt 0 ]; do case "$1" in
  --instance) INSTANCE="$2"; shift 2;; --packet) PACKET="$2"; shift 2;; --out) OUT="$2"; shift 2;;
  *) shift;; esac; done
[ -z "$INSTANCE" -o -z "$PACKET" -o -z "$OUT" ] && { echo "usage: --instance R --packet F --out D"; exit 2; }
PROMPTS="$INSTANCE/.research-os/prompts/committee"
[ -d "$PROMPTS" ] || PROMPTS="${RESEARCH_OS_PROMPTS:-$(dirname "$0")/../prompts}/committee"
mkdir -p "$OUT"

# read committee members (role backend) from config via python (pyyaml)
mapfile -t MEMBERS < <(python3 - "$INSTANCE/research-os.config.yaml" <<'PY'
import sys,yaml
c=yaml.safe_load(open(sys.argv[1])) or {}
for m in (c.get("committee") or {}).get("members",[]):
    print(f"{m.get('role')} {m.get('backend')}")
PY
)

run_one(){ # role backend
  local role="$1" backend="$2"
  local sysp; sysp="$(cat "$PROMPTS/${role}_v001.md" "$PROMPTS/_committee_common_v001.md" 2>/dev/null)"
  local full="$sysp

=== REVIEW PACKET ===
$(cat "$PACKET")
=== END PACKET ===
Output ONLY your structured vote block for role $role."
  # invoke by CLI shape inferred from backend id (configurable; extend as needed)
  case "$backend" in
    claude-*|opus*|sonnet*) claude --model "$backend" -p "$full" </dev/null >"$OUT/${role}.out" 2>"$OUT/${role}.err" ;;
    codex*)                 codex exec --skip-git-repo-check "$full" </dev/null >"$OUT/${role}.out" 2>"$OUT/${role}.err" ;;
    gemini*)                gemini -p "$full" >"$OUT/${role}.out" 2>"$OUT/${role}.err" ;;
    *)                      "${COMMITTEE_GENERIC_CLI:-cat}" >"$OUT/${role}.out" 2>"$OUT/${role}.err" <<<"$full" ;;
  esac
  echo "  done: $role ($backend)"
}
for line in "${MEMBERS[@]}"; do
  set -- $line; run_one "$1" "$2" &
done
wait
echo "ALL_COMMITTEE_DONE" > "$OUT/_status.txt"
