"""Sandbox worker: python -m plaindaw.plugins.worker job.pkl result.pkl"""
import pickle
import sys

from .host import render_plugin


def main() -> int:
    job = pickle.loads(open(sys.argv[1], "rb").read())
    res = render_plugin(job)
    with open(sys.argv[2], "wb") as f:
        pickle.dump(res, f, protocol=pickle.HIGHEST_PROTOCOL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
