# research-os Architecture (v0)

## Engine vs Instance (the key split)
- **research-os** (this repo) = generic engine. Schemas, templates, versioned prompts, rule config, CLI.
  ZERO research content. Platform-agnostic: nodes/backends/GPU/budgets are CONFIG.
- **`<you>/autonomous-research`** = instance. The durable research state for one user/org.
  Created by `ros init`; configured by `research-os.config.yaml`.

## First-class durable objects (all in the instance's registry/, append-only per-object files)
claims/ · experiments/ · verdicts/ · cemetery/ + academic_map.yaml · baselines.yaml · projects.yaml
Sessions are raw execution logs (forensic only) — never the source of truth.

## The two hard invariants (enforced by the engine CLI)
1. **No experiment runs unregistered** — `ros exp register` mints the EXP-id; it's the only door to compute.
2. **No cemetery idea resurrects** — `ros seed new` checks the dead-idea registry (Jaccard + asymmetric
   containment; hard-refuse ≥0.7, soft-refuse 0.45–0.7 unless --ack-dup; --force-revive needs new evidence).

## Lifecycle
seed → exploration → candidate → paper-track → archived. Cheap to open a seed; expensive (hostile
committee) to promote. Every cycle ends commit-and-{kill|weaken|keep-exploring|promote|archive}.

## Roles
- **Orchestrator** (hybrid): script owns ticks/liveness/ledger; LLM invoked only for judgment (prompts/orchestrator/v001.md).
- **Session agents**: bounded execution (probe/bench/impl/lit/synthesis). Register outputs; never treat notes as truth.
- **Local skeptic**: cheap pre-triage; cemetery-dup + boundedness check before orchestrator.
- **Hostile committee** (logical-standing, physically-on-demand): 6 versioned roles, spawned per review,
  torn down after; reboot = spawn fresh from compressed packet + rubric. Parallel-safe via documented rubric.
- **Monitors/observer**: liveness + safety + drift audit (prompts/monitors/orchestrator_monitor_v001.md).

## Liveness (push-based, patient)
Each agent kicks the supervisor every ~15m. The supervisor does NOT poll-kill. An agent is not declared
dead before the grace window (default 45m) — reasoning/long-runs are normal. runtime/ lives on a STABLE
control node (a laptop), never a fragile GPU node, so kernel crashes on compute nodes don't lose control state.

## Runtime vs durable
- runtime/ (control node, git-ignored in engine): heartbeats, job_queue, active_jobs, scratch.
- registry/ + experiments/ + verdicts/ + cemetery/ (git): scientific state. Completed experiments MUST commit.

## Safety (learned the hard way)
VMM/large-alloc probes crash nodes at ALLOCATION and TEARDOWN. Rule: cap small, watchdog host-RAM on BOTH
ends, use os._exit() to skip per-object teardown, read learning/ first, never autonomously trigger node repairs.

## MVP status (v0)
Built + validated live: engine CLI (init/seed/exp register/exp complete/status), both invariants, schemas,
orchestrator + monitor prompts, config schema. Planned next: committee runner, prior-art scout, scheduler
tick, push-liveness daemon, end-of-day digest. See docs/MVP.md.
