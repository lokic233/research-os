# research-os v2 — channeling, generic topology, queue-decoupled runners

Status: DESIGN (staged rollout; additive; running orchestrator-r3 unaffected until reboot).
Author: monitor 6433b2c0 per dengcchi 2026-06-01.

## Goals
1. ONE generic queue/inbox abstraction (`engine/channeling/`) shared by every producer→consumer link.
2. A generic, config-driven **backend registry** (cpu/gpu) — topology logic, not hardcoded nodes.
3. **Decouple the GPU runner from the orchestrator** via a queue, exactly like sub-monitors decoupled
   researchers. One `gpu_coordinator` (a.k.a. task_coordinator) per GPU backend.
4. Move all environment/config + the global retire threshold into the PRIVATE `research-os-meta` repo.
5. Rename `prompts/` → `role/` and fold `rules/` into it (roles + their rules co-located).
6. Instance repo gets a `/topology/` folder describing each project's live agent control graph.

## The control topology (per project)
```
monitor ──supervises──> orchestrator ──supervises──> sub-monitor ──supervises──> researcher (xN)
                              │
                              ├──(committee channel)──> honest 6-model committee
                              └──(gpu task channel)───> gpu_coordinator (1 per GPU) ──> GPU exp runner
```
Each ──> that crosses an agent boundary is a **channel** (queue/inbox). Supervision = the parent runs a
discovery/healthcheck on a cadence and respawns a dead child (dead children can't respawn themselves).

## 1. engine/channeling/  (the shared abstraction)
A `Channel` = an append-only, YAML-backed list of items with ack semantics. Atomic writes (reuse ros
load/dump). Backward-compatible with the two EXISTING structures:
  - orchestrator inbox  : runtime/orchestrator/inbox.yaml          list_key="items"  (report→orchestrator)
  - committee queue     : runtime/orchestrator/committee_queue.yaml list_key="queue"  (sub-monitor→orch)
  - NEW gpu task channel: runtime/orchestrator/gpu_tasks.yaml       list_key="tasks"  (orch→gpu_coordinator)
  - NEW gpu result chan : runtime/orchestrator/gpu_results.yaml     list_key="results"(coordinator→orch)
API: Channel(path, list_key).submit(rec)->id ; .list(include_acked, action_only) ; .ack(id|all, by, answer)
   ; .find(predicate) for idempotency. Item envelope: {id, submitted_at, acked, acked_at, acked_by, answer, **payload}.

## 2. Backend registry (queue-based, config-driven)
`compute_nodes` in config already lists {name,kind,gpu_type,host_mem_gb,fragile,probe_cmd}. v2 adds a
RUNTIME registry runtime/registry/backends.yaml: each backend self-registers (like agents) + carries
{status, current_task, lease, last_heartbeat}. `ros backend register|list|heartbeat`. The gpu task channel
is the queue; the registry is the table of who can pull from it. Generic over cpu/gpu kind.

## 3. gpu_coordinator (task_coordinator) — 1 per GPU backend
Role prompt role/coordinator/gpu_coordinator.md. ACTIVE runner loop:
  - register backend; pull next matching task from the gpu task channel (respect gpu_type + fragile
    host_mem_floor safety; NEVER bypass watchdog on MI350X).
  - dispatch on its GPU, ACTIVELY poll to completion (GPU is fragile → tight monitoring), audit + bug-report
    on fault (write learning/GPU_BUGLOG_<coordinator-id>.md), release lease on done/fault.
  - loop the artifact back to the orchestrator via the gpu result channel; orchestrator processes results
    1-by-1 (ros gpu-results list → exp complete → verdict). No human approval as long as GPU is idle+task queued.
  - self-retire + graceful handoff at the global 35% threshold.
Orchestrator supervises coordinators: `ros coordinators` discovery (mirror of `ros submonitors`) → respawn
any GPU whose coordinator is MISSING/DEAD.

## 4. research-os-meta (private) — all env/config + globals
Move the live research-os.config.yaml's node/backend/cert specifics into research-os-meta. The public engine
stays node-agnostic. NEW global var: `globals.retire_at_context_pct: 35` (350k tokens) — the single source
for ALL long-running/standby agents (monitor, orchestrator, sub-monitor, gpu_coordinator). Engine reads it
from config (which the instance pulls from meta); default 35 if absent.

## 5. prompts/ → role/  + rules/ fold-in
`git mv prompts role`. Each role dir keeps its prompt + a co-located `rules.md` (the bits of the old top-level
rules/ that pertain to that role). Update all path refs (orchestrator spawns read role/...). Keep history.

## 6. instance /topology/
autonomous-research/topology/<PROJ>/topology.yaml — the live control graph for that project: which
orchestrator, sub-monitor, researchers, coordinators, channels are wired, with their session ids + status.
Rendered/maintained like progress_report.md. Gives a per-project at-a-glance of the agent tree.

## Staged rollout (bugbash each before the next)
- A. engine/channeling/ + refactor existing inbox+queue onto it (backward compat). ← do first, test live cmds.
- B. backend registry (`ros backend ...`) + gpu task/result channels.
- C. gpu_coordinator role + `ros coordinators` discovery + orchestrator prompt wiring.
- D. prompts→role rename + rules fold-in + path-ref fixes.
- E. config→meta + global retire var.
All additive; the running orchestrator-r3 keeps its current model until it reboots into the new role prompts.
