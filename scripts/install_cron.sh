#!/usr/bin/env bash
# Install a daily midnight cron job for job_monitor.py --once
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
LOG_FILE="${ROOT_DIR}/job_monitor.log"
CRON_TAG="# job-monitor-daily"
CRON_HOUR="${SCHEDULE_HOUR:-0}"
CRON_MINUTE="${SCHEDULE_MINUTE:-0}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Error: ${PYTHON_BIN} not found."
  echo "Create the venv first:"
  echo "  cd ${ROOT_DIR}"
  echo "  python3 -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  pip install -r requirements.txt"
  echo "  playwright install chromium"
  exit 1
fi

if [[ ! -f "${ROOT_DIR}/.env" ]]; then
  echo "Warning: ${ROOT_DIR}/.env is missing."
  echo "Copy .env.example to .env and fill in your keys before the first midnight run."
fi

if ! command -v crontab >/dev/null 2>&1; then
  echo "Error: crontab is not available on this system."
  echo "On macOS/Linux it is usually preinstalled. On some Linux servers install cronie/cron."
  exit 1
fi

# Run from the project directory so .env / seen_jobs.json resolve correctly.
CRON_LINE="${CRON_MINUTE} ${CRON_HOUR} * * * cd \"${ROOT_DIR}\" && \"${PYTHON_BIN}\" job_monitor.py --once >> \"${LOG_FILE}\" 2>&1 ${CRON_TAG}"

EXISTING="$(crontab -l 2>/dev/null || true)"

# Remove any previous job-monitor-daily lines, then add the new one.
FILTERED="$(printf '%s\n' "${EXISTING}" | grep -v "${CRON_TAG}" || true)"
{
  if [[ -n "${FILTERED}" ]]; then
    printf '%s\n' "${FILTERED}"
  fi
  printf '%s\n' "${CRON_LINE}"
} | crontab -

echo "Installed daily cron job:"
echo "  ${CRON_LINE}"
echo
echo "It will run every day at ${CRON_HOUR}:$(printf '%02d' "${CRON_MINUTE}") local time."
echo "Logs append to: ${LOG_FILE}"
echo
echo "Useful commands:"
echo "  crontab -l                 # list scheduled jobs"
echo "  ./scripts/uninstall_cron.sh"
echo "  tail -f ${LOG_FILE}        # watch the next run"
