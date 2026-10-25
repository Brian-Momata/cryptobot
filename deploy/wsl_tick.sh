#!/usr/bin/env bash
# One hourly tick when running on a Windows PC under WSL. Windows Task Scheduler runs this
# (see docs/deploy.md, "Running on your PC"). Output goes to data/live.log.
cd "$(dirname "$0")/.." || exit 1
.venv/bin/python live.py --once >> data/live.log 2>&1
