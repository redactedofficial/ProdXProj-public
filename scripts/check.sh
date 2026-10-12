#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
runner_python=python3
if [ -x .venv/bin/python ]; then runner_python=.venv/bin/python; fi
"$runner_python" -m unittest discover -s tests -v
"$runner_python" -m compileall -q proxrunner tests
