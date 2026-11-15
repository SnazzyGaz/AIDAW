"""Run plugin jobs in a separate process so a crashing plugin cannot take down
the session. Falls back to a silent result + warning on crash or timeout."""
from __future__ import annotations

import os
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path


class PluginCrashed(RuntimeError):
    pass


def run_job(job: dict, sandbox: bool = True, timeout: float = 600.0) -> dict:
    if not sandbox:
        from .host import render_plugin
        return render_plugin(job)
    with tempfile.TemporaryDirectory(prefix="plaindaw-job-") as td:
        jp, rp = Path(td) / "job.pkl", Path(td) / "result.pkl"
        jp.write_bytes(pickle.dumps(job, protocol=pickle.HIGHEST_PROTOCOL))
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            p = subprocess.run([sys.executable, "-m", "plaindaw.plugins.worker", str(jp), str(rp)],
                               capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise PluginCrashed(f"plugin timed out after {timeout}s: {job.get('path')}")
        if p.returncode != 0 or not rp.exists():
            tail = p.stderr.decode(errors="replace").strip().splitlines()[-3:]
            raise PluginCrashed(f"plugin process exited with code {p.returncode}: {job.get('path')} "
                                f"{' | '.join(tail)}")
        return pickle.loads(rp.read_bytes())
