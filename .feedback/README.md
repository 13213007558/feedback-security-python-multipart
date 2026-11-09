# Feedback task starting environment

Upstream: https://github.com/Kludex/python-multipart
Upstream commit: `6cebfd8439763215fa5d07283ff4ce716e31b491`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.
The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= tests
```

Observed baseline (2026-11-09): `184 passed, 1 warning in 2.26s`
Whole-command wall time: 2.830 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.
