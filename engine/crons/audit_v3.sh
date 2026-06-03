#!/bin/bash
# audit_v3.sh — deep consistency audit for a v3 instance (dengcchi's directive: catch hidden drift each
# bugbash cycle). Read-only. Compares LOCAL working tree vs LAST COMMIT vs REMOTE, and validates the
# date-separation registry invariant. Exits non-zero if any drift/mess is found.
set -u
E="${1:-/Users/dengcchi/autonomous-research-v3}"
cd "$E" || { echo "FATAL: no $E"; exit 2; }
FAIL=0
echo "=== AUDIT $(date -u +%Y-%m-%dT%H:%M:%SZ) instance=$E ==="

echo "--- [1] uncommitted local work (on disk, not in git) ---"
U=$(git status --porcelain | grep -v '^!!' | wc -l | tr -d ' ')
if [ "$U" != "0" ]; then echo "  ⚠️ $U uncommitted change(s):"; git status --porcelain | head -20; FAIL=1; else echo "  ✅ clean"; fi

echo "--- [2] local HEAD vs remote (committed-but-unpushed) ---"
git fetch origin -q 2>/dev/null
BR=$(git branch --show-current)
L=$(git rev-parse --short HEAD); R=$(git rev-parse --short "origin/$BR" 2>/dev/null || echo NONE)
if [ "$L" != "$R" ]; then echo "  ⚠️ local=$L remote($BR)=$R — unpushed:"; git log --oneline "origin/$BR..HEAD" 2>/dev/null | head; FAIL=1; else echo "  ✅ local==remote ($L on $BR)"; fi

echo "--- [3] date-separation: dir-date == internal date: field (verdicts) ---"
N=0
for f in $(find registry/verdicts -name 'VERDICT-*.yaml' 2>/dev/null); do
  dd=$(echo "$f" | sed -E 's#.*/([0-9]{4}-[0-9]{2}-[0-9]{2})/.*#\1#')
  fd=$(grep -E "^date:" "$f" | head -1 | sed -E "s/date:[[:space:]]*'?([0-9-]+)'?.*/\1/")
  if [ -n "$fd" ] && [ "$dd" != "$fd" ]; then echo "  ⚠️ $(basename $f): dir=$dd field=$fd"; N=$((N+1)); FAIL=1; fi
done
[ "$N" = "0" ] && echo "  ✅ all verdict dir-date == field-date"

echo "--- [4] no duplicate object IDs scattered across date dirs ---"
D=0
for kind in claims verdicts; do
  dup=$(find registry/$kind -name '*.yaml' 2>/dev/null | sed -E 's#.*/([A-Z]+-[0-9]+)\.yaml#\1#' | sort | uniq -d)
  [ -n "$dup" ] && { echo "  ⚠️ duplicate $kind: $dup"; D=1; FAIL=1; }
done
dupe=$(find experiments -name experiment.yaml 2>/dev/null | sed -E 's#.*/(EXP-[0-9]+)/.*#\1#' | sort | uniq -d)
[ -n "$dupe" ] && { echo "  ⚠️ duplicate exp: $dupe"; D=1; FAIL=1; }
[ "$D" = "0" ] && echo "  ✅ no duplicate IDs"

echo "--- [5] every registry object is git-tracked (not silently ignored) ---"
UNT=$(git status --porcelain registry experiments projects 2>/dev/null | grep -E '^\?\?' | wc -l | tr -d ' ')
[ "$UNT" != "0" ] && { echo "  ⚠️ $UNT untracked registry/exp/proj file(s):"; git status --porcelain registry experiments projects | grep '^??' | head; FAIL=1; } || echo "  ✅ all tracked"

[ "$FAIL" = "0" ] && echo "=== ✅ AUDIT CLEAN ===" || echo "=== ⚠️ AUDIT FOUND DRIFT (see above) ==="
exit $FAIL
