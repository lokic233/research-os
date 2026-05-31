# Operational lesson: orchestrator goes idle WHILE WAITING on a spawned agent

## What happened (full-cycle PoC, 2026-05-31)
Orchestrator did everything right up to a point: spawned lit-miner -> 3 real prior-art findings -> opened
CLAIM-0002 -> spawned experiment_runner. Then its TURN ENDED while "waiting." The runner FINISHED (EXP-0001
completed, effect=support, "idle window hides prefill 93% of the time"), but nothing woke the orchestrator,
so it never convened the committee. Stalled at 2 claims / 1 exp / 0 verdicts for ~17 min until nudged.

## Root cause
Two failure modes, same symptom (idle orchestrator):
1. (earlier, fixed) Researchers only HEARTBEAT, never REPORT progress -> orchestrator has no signal. Fixed
   with `ros report` / `ros inbox`.
2. (this one) Orchestrator SPAWNS a child then ENDS ITS TURN to "wait." A turn that ends is DONE, not
   sleeping. No event auto-resumes it when the child finishes. It waits forever.

## Fix (three complementary mechanisms)
A. Child completion wakes the parent: a finishing researcher (new_session) must `agent_run.message` the
   orchestrator ("EXP-X done, effect=Y, resume"). Push, not poll. (Sub-agents auto-announce; new_session
   children must explicitly ping.) -> researcher prompt finish-step.
B. Orchestrator must not end its turn while work is outstanding: poll in a loop within the same turn
   (sleep + check ros inbox/liveness/artifact) OR self-schedule a wake-up. Ending the turn = idle.
   -> orchestrator prompt.
C. Supervisor tick (scheduled monitor) is the backstop: detect completed-but-unprocessed state (exp
   completed but claim not advanced; committee run with no verdict) and AUTO-REVIVE the orchestrator via
   agent_run.message. The 10-min monitor already detects the symptom; extend it to auto-revive.
