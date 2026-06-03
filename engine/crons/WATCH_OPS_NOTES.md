# v3 WATCH OPS NOTES (read before each debug cycle)

## Tool paths (ABSOLUTE — do not search $E/$HOME)
- audit:  bash /Users/dengcchi/research-os/engine/crons/audit_v3.sh
- v2diff: bash /Users/dengcchi/research-os/engine/crons/diff_v2_v3.sh
  (these live in the ENGINE repo engine/crons/, NOT in the v3 instance $E)

## Claiming a BUG-NN under concurrent debugger sessions (avoids the 101/102 double-collisions)
1. `git -C /Users/dengcchi/autonomous-research log --oneline --grep "BUG-" | head` AND
   `git -C /Users/dengcchi/research-os log --oneline --grep "BUG-" | head` to find the highest used N.
2. RESERVE it FIRST: append a one-line stub to the v2 buglog `## BUG-<N+1> (RESERVED <session> <ts>)`,
   commit+push that buglog line BEFORE writing the engine fix. The pushed buglog line is the atomic claim.
3. Then make the engine fix referencing that N. If your push of the reservation rejects (someone took it),
   bump and retry. Never put the number only in an engine commit msg (immutable + unsynced = collisions).

## Engine working tree is SHARED across cycles
- `git -C /Users/dengcchi/research-os status` before editing. If dirty = another session mid-edit ->
  bugbash in /tmp only, do NOT commit engine. `git diff` before commit; never blanket-add another's hunk.

## Human-gated quiescence
- If QUIESCENCE_*.md is open in $E and topic-bias unchanged, the orchestrator HOLDS (no new projects).
  Do not nudge it to seed. Surface the fork to dengcchi; wait.
