# research-os

**An engine for running autonomous, self-correcting research — with claims, experiments, verdicts,
and a hostile review committee as first-class, durable, version-controlled objects.**

research-os is the *generic engine*. It holds no research content. You instantiate it into your own
**instance repo** (`<you>/autonomous-research`) that holds the actual durable research state.

```
research-os (this repo, generic)            <you>/autonomous-research (your instance)
  schemas/      — object schemas              registry/   — YOUR claims/experiments/verdicts/cemetery
  templates/    — object templates            projects/   — YOUR project lifecycle state
  prompts/      — versioned role prompts       prior_art/  — YOUR prior-art packets
  rules/        — lifecycle + budget + gates    sessions/   — raw execution logs (forensic only)
  engine/       — the CLI + invariant gates     learning/   — YOUR crash postmortems / lessons
  init/         — `research-os init` scaffolder  research-os.config.yaml — points at engine + YOUR nodes/backends/budgets
```

## Core philosophy
- **Creativity is cheap and permissive.** Opening a seed costs almost nothing.
- **Commitment is expensive and hostile.** Promotion requires surviving a hostile committee.
- **Sessions are execution logs, not the unit of research.** State lives in registries, not chat context.
- **Every full cycle ends in commit-and-{kill, weaken, keep-exploring, promote, archive}** — never raw logs.
- **Dead ideas stay dead.** The cemetery prevents accidental resurrection.
- **No unregistered compute.** Every experiment has an EXP-id before it runs.
- **Platform-agnostic.** Compute nodes, model backends, GPU types, and budgets are CONFIG, not code.

## Two hard invariants (the spine)
1. **No experiment runs unregistered** — registration is the only door to compute.
2. **No cemetery idea resurrects** — seeds are checked against the dead-idea registry.

## Quick start
```bash
# in your instance repo:
python <path-to-research-os>/engine/ros.py init       # scaffold registry/, config, dirs
python <path-to-research-os>/engine/ros.py seed new    # open a cheap seed (cemetery-checked)
python <path-to-research-os>/engine/ros.py exp register # register an experiment (the only door to compute)
python <path-to-research-os>/engine/ros.py exp complete # commit-and-{kill|promote|...}; forces durable state
```

## Status
v0 MVP — lean spine. See `docs/ARCHITECTURE.md` for the full design and `docs/MVP.md` for what's built vs planned.
Design source: the research-OS design doc (formalizing lessons from real autonomous-research runs).
