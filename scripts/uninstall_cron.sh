#!/usr/bin/env bash
# Remove the job_monitor daily cron job installed by install_cron.sh
set -euo pipefail

CRON_TAG="# job-monitor-daily"

if ! command -v crontab >/dev/null 2>&1; then
  echo "Error: crontab is not available on this system."
  exit 1
fi

EXISTING="$(crontab -l 2>/dev/null || true)"
if ! printf '%s\n' "${EXISTING}" | grep -q "${CRON_TAG}"; then
  echo "No job-monitor cron entry found. Nothing to uninstall."
  exit 0
fi

FILTERED="$(printf '%s\n' "${EXISTING}" | grep -v "${CRON_TAG}" || true)"
if [[ -z "${FILTERED}" ]]; then
  # Empty crontab — clear it
  crontab -r 2>/dev/null || true
else
  printf '%s\n' "${FILTERED}" | crontab -
fi

echo "Removed daily job-monitor cron job."
echo "Current crontab:"
crontab -l 2>/dev/null || echo "(empty)"
