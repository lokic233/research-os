#!/bin/bash
# diff_v2_v3.sh — compare v3 against the hardened v2 instance (golden reference) to surface maturity gaps.
# Read-only. v2=/Users/dengcchi/autonomous-research (frozen, mature). v3=/Users/dengcchi/autonomous-research-v3.
set -u
V2=/Users/dengcchi/autonomous-research; V3=/Users/dengcchi/autonomous-research-v3
echo "=== V2-vs-V3 DIFF $(date -u +%H:%M:%SZ) ==="

echo "--- top-level dirs in V2 missing from V3 ---"
comm -23 <(ls "$V2" | sort) <(ls "$V3" | sort) | sed 's/^/  V2-only: /'

echo "--- claim schema: keys on a mature V2 claim missing from a recent V3 claim ---"
V2C=$(find "$V2/registry/claims" -name 'CLAIM-*.yaml' | sort | tail -1)
V3C=$(find "$V3/registry/claims" -name 'CLAIM-*.yaml' | sort | tail -1)
comm -23 <(grep -oE '^[a-z_]+:' "$V2C" 2>/dev/null | sort -u) <(grep -oE '^[a-z_]+:' "$V3C" 2>/dev/null | sort -u) | sed 's/^/  claim V2-only key: /'

echo "--- verdict schema: keys on a mature V2 verdict missing from a recent V3 verdict ---"
V2V=$(find "$V2/registry/verdicts" -name 'VERDICT-*.yaml' | sort | tail -1)
V3V=$(find "$V3/registry/verdicts" -name 'VERDICT-*.yaml' | sort | tail -1)
comm -23 <(grep -oE '^[a-z_]+:' "$V2V" 2>/dev/null | sort -u) <(grep -oE '^[a-z_]+:' "$V3V" 2>/dev/null | sort -u) | sed 's/^/  verdict V2-only key: /'

echo "--- registry layout: both use <PROJ>/<YYYY-MM-DD>/ ? ---"
echo "  V2 claims sample: $(find "$V2/registry/claims" -name 'CLAIM-*.yaml' | head -1 | sed -E 's#.*/registry/claims/##')"
echo "  V3 claims sample: $(find "$V3/registry/claims" -name 'CLAIM-*.yaml' | head -1 | sed -E 's#.*/registry/claims/##')"

echo "=== (empty 'V2-only' lines = v3 at parity on that dimension) ==="
