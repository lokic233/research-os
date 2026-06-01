"""engine/channeling — the ONE generic queue/inbox abstraction for research-os agent links.

Every producer->consumer hop across an agent boundary is a Channel: an append-only, YAML-backed list
of envelopes with ack semantics + atomic writes + idempotency. Used by:
  - orchestrator inbox        (researcher/sub-monitor report -> orchestrator)   list_key="items"
  - committee submission queue(sub-monitor -> orchestrator)                     list_key="queue"
  - gpu task channel          (orchestrator -> gpu_coordinator)                 list_key="tasks"
  - gpu result channel        (gpu_coordinator -> orchestrator)                 list_key="results"
Stdlib + pyyaml only. Atomic writes mirror engine/ros.py (tmp + os.replace).
"""
from .channel import Channel, NOW
