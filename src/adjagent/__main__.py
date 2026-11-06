import argparse
from pathlib import Path

from . import __version__
from .agent import Run, make_llm
from .outputs import write_all


def main(argv=None):
    p = argparse.ArgumentParser(prog="adjagent", description="Validate a batch of manual journal entries")
    p.add_argument("--inputs", default="inputs", type=Path)
    p.add_argument("--policy", default="config/policy.json", type=Path)
    p.add_argument("--overrides", type=Path, help="JSON file of human decisions to apply")
    p.add_argument("--out", default="output", type=Path)
    p.add_argument("--llm", action="store_true", help="write explanations with Claude (needs ANTHROPIC_API_KEY)")
    a = p.parse_args(argv)
    llm, label = make_llm() if a.llm else (None, "")
    run = Run(a.inputs, a.policy, a.overrides, llm, label)
    res = run.execute()
    res["code_version"] = __version__
    write_all(res, run, a.out)
    counts = {}
    for d in res["decisions"].values():
        counts[d.status.value] = counts.get(d.status.value, 0) + 1
    print(f"run {run.run_id}: {counts} -> {a.out}/")
    for k, v in res["invariants"].items():
        if k.startswith("I"):
            print(f"  {'PASS' if v else 'FAIL'} {k}")


if __name__ == "__main__":
    main()
