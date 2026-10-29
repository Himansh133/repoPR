# 015: Python dependencies as wheels, installed outside the repo

**Decision:** When a repo has a `requirements.txt`, the `python-pytest` profile installs it once with `pip install --only-binary=:all: --target /deps`, in a separate container with network. Only plain requirement lines are accepted. The packages go into a folder next to the working copy, which the offline test container mounts read-only on `PYTHONPATH`. The folder is deleted when the run ends.

**Why:** Wheels-only is the Python equivalent of `npm ci --ignore-scripts`: an sdist or a local path would run a build backend, which is arbitrary package code with network. Refusing options closes the ways a file could undo that (`--no-binary`, `-e .`, another index). Keeping the packages outside the repo means the search and read tools never see them and they can never reach a patch or a PR, so nothing else has to filter them out.

**Cost:** Packages without a wheel for Linux and Python 3.12 cannot be installed, and the run aborts with pip's message. Dependencies declared only in `pyproject.toml` or `setup.py` are not installed, because reading them needs a build. Unpinned requirements resolve to whatever is newest at run time.
