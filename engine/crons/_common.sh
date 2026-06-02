#!/bin/bash
# _common.sh — shared helpers for v3 deterministic cron scripts. Sourced, not run.
# Contract: each cron drops runtime/cron/<job>.alive ON SUCCESS only (so ros cron-health can detect a
# dead/stalled cron). A cron that errors must NOT stamp .alive (let it go stale -> monitor escalates).
set -u
: "${ROS_INSTANCE:?set ROS_INSTANCE to the instance root}"
PYBIN="${ROS_PYTHON:-/usr/bin/python3}"
ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROS() { "$PYBIN" "$ENGINE_DIR/ros.py" --instance "$ROS_INSTANCE" "$@"; }
RUNTIME_DIR() {
  "$PYBIN" - "$ROS_INSTANCE" <<'PY'
import sys,os
try: import yaml
except Exception: yaml=None
root=sys.argv[1]; rd=os.path.join(root,"runtime")
if yaml:
    c=yaml.safe_load(open(os.path.join(root,"research-os.config.yaml"))) or {}
    rd=os.path.abspath(((c.get("runtime") or {}).get("runtime_dir")) or rd)
print(rd)
PY
}
CRON_DIR="$(RUNTIME_DIR)/cron"
mkdir -p "$CRON_DIR"
stamp_alive() { date -u +%Y-%m-%dT%H:%M:%SZ > "$CRON_DIR/$1.alive"; }   # call ONLY on success
log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }
