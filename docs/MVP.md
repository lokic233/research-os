# MVP status

## Done (validated by a real lifecycle run)
- [x] Engine CLI `engine/ros.py`: init, seed new, exp register, exp complete, status
- [x] INVARIANT 1: no experiment without a registered claim (refuses unknown claim)
- [x] INVARIANT 2: cemetery dup-check refuses rephrased dead ideas (validated: 0.78–0.89 scores caught)
- [x] Auto-bury on kill (claim→cemetery with revival conditions)
- [x] Commit-message generation (commit-and-{effect})
- [x] All 6 object schemas + config schema
- [x] Orchestrator v001 + Orchestrator-Monitor v001 prompts

## Next (in priority order)
1. `ros init` in the real instance (lokic233/autonomous-research) + research-os.config.yaml
2. Migrate the 3 existing GREEN theses + dead ideas as seed claims + cemetery entries
3. Committee runner (spawn 6 on-demand, collect votes, area-chair aggregate, write VERDICT)
4. Push-liveness: agent kick subagent + supervisor heartbeat ledger on control node
5. Scheduler tick (idle-GPU opportunistic Level-0/1 dispatch, with safety gate)
6. Prior-art scout + packet; end-of-day digest
