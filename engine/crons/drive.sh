#!/bin/bash
# drive.sh — v3 cron driver. Two modes:
#   drive.sh once          run each of the 4 crons exactly once (STANDBY VALIDATION — safe, no scheduler)
#   drive.sh crontab       print the crontab block to install for live cutover (does NOT install)
#   drive.sh launchd       print a launchd plist set for macOS cutover (does NOT install)
# Requires ROS_INSTANCE (the v3 instance root) and optionally ROS_PYTHON.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${ROS_INSTANCE:?set ROS_INSTANCE to the v3 instance root}"
PYBIN="${ROS_PYTHON:-/usr/bin/python3}"
mode="${1:-once}"
case "$mode" in
  once)
    echo "=== v3 STANDBY VALIDATION: one cycle of all 4 crons (instance=$ROS_INSTANCE) ==="
    for c in proj_monitor committee_health coordinator monitor; do
      echo "--- $c ---"; ROS_INSTANCE="$ROS_INSTANCE" ROS_PYTHON="$PYBIN" bash "$HERE/$c.sh"; echo "  exit=$?"
    done
    echo "=== cron-health snapshot ==="
    "$PYBIN" "$HERE/../ros.py" --instance "$ROS_INSTANCE" cron-health
    ;;
  crontab)
    echo "# v3 research-os crons (install with: crontab -e). All deterministic scripts."
    echo "*/5 * * * * ROS_INSTANCE=$ROS_INSTANCE $HERE/proj_monitor.sh >> $ROS_INSTANCE/runtime/cron/proj_monitor.log 2>&1"
    echo "*/1 * * * * ROS_INSTANCE=$ROS_INSTANCE $HERE/committee_health.sh >> $ROS_INSTANCE/runtime/cron/committee_health.log 2>&1"
    echo "*/2 * * * * ROS_INSTANCE=$ROS_INSTANCE $HERE/coordinator.sh >> $ROS_INSTANCE/runtime/cron/coordinator.log 2>&1"
    echo "*/5 * * * * ROS_INSTANCE=$ROS_INSTANCE $HERE/monitor.sh >> $ROS_INSTANCE/runtime/cron/monitor.log 2>&1"
    ;;
  launchd)
    echo "# macOS launchd: write each as ~/Library/LaunchAgents/com.researchos.v3.<cron>.plist with StartInterval"
    echo "# proj_monitor=300s committee_health=60s coordinator=120s monitor=300s; ProgramArguments=[bash, $HERE/<cron>.sh]"
    echo "# EnvironmentVariables: ROS_INSTANCE=$ROS_INSTANCE  (then launchctl load each). NOT installed by this script."
    ;;
  *) echo "usage: drive.sh once|crontab|launchd"; exit 2;;
esac
