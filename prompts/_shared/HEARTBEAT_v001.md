# Heartbeat sub-agent pattern — v001
Every spawned agent (committee member OR researcher) MUST keep the orchestrator aware it is alive,
WITHOUT the orchestrator polling (which can't tell "reasoning/long-run" from "dead").

## The pattern (push-based, via a tiny sub-agent)
On spawn, an agent does two things:
1. `ros agent register --id <agent_id> --role <role> [--backend ..] [--node ..] [--claim ..] [--exp ..]`
2. Launches a lightweight HEARTBEAT SUB-AGENT (or a background `while` loop) that, every
   `liveness.kick_interval_minutes` (default 15), runs:
       ros heartbeat --agent <agent_id> --status running --note "<one-line what I'm doing>"
   The heartbeat sub-agent does nothing else — it is cheap and only reports liveness. It keeps kicking
   even while the main agent is deep in reasoning or a long experiment.
3. On finish/fail: `ros heartbeat --agent <agent_id> --status completed|failed`.

## Orchestrator side (patient)
- Reads `ros liveness`. An agent is alive if kicked within ~1.5× the interval, **stale** within the grace
  window, **DEAD** only past `patient_grace_minutes` (default 45). The orchestrator NEVER kills/revives a
  merely-stale agent — long runs and reasoning are normal. Only past grace does it revive.
- Reviving a committee member = spawn fresh from the role prompt + compressed packet (NOT the old long session).

## Why a sub-agent (not the main agent self-reporting)
A main agent blocked in a 40-minute experiment or a long reasoning turn cannot also emit heartbeats. A
separate cheap heartbeat process guarantees liveness signal is independent of the main agent's busy state.
This is exactly why poll-based death detection fails and push-based is required.
