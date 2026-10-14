"""Check whether the CP scheduler (convert_plan_to_tors/schedule.py) can re-time
the converted plans of an experiment run, under a few variants of its model.

Run it inside the planner image, which has ortools and the code at /app:

    docker run --rm --entrypoint python3 \
      --mount type=bind,source=<location dir>,target=/app/database,readonly \
      --mount type=bind,source=<results/<experiment name>>,target=/app/results,readonly \
      --mount type=bind,source=<this folder>,target=/app/io,readonly \
      planner:latest /app/io/diagnose_cp.py

For every scenario with a converted plan (planning/plan.json) it prints the
solver status for:

  GAP=1        the model as committed (one second between a unit's actions)
  GAP=0        no gap, matching the current converter
  GAP=0 noWait no gap, and the converter's fixed-length Wait actions left out,
               so waiting is free to stretch or shrink
  pinnedExits  as GAP=0 noWait, with every Exit fixed at the departure time of
               its own request (only for plans TORS rejected)

next to the TORS verdict from planning/eval_result.json.
"""

import collections
import copy
import glob
import json
import os
import sys

sys.path.insert(0, "/app")
import convert_plan_to_tors.schedule as schedule  # noqa: E402

LOCATION = "/app/database/location.json"
RESULTS = "/app/results"


def solve(actions, scenario, location, gap, drop_waits):
    schedule.GAP = gap
    if drop_waits:
        actions = [a for a in actions if a["taskType"].get("predefined") != "Wait"]
    return schedule.assign_times(copy.deepcopy(actions), scenario, location)["status"]


def pin_exits_to_requests(actions, scenario):
    """Set every Exit's time to the departure of the request it serves.

    The plan does not name the request, so match on composition (unit types,
    order ignored) and take the unused request nearest in time, which is how
    the experiment's late departures line up with their requests.
    """
    unit_type = {member["id"]: (member["typePrefix"], member["carriages"])
                 for group in ("in", "inStanding")
                 for train in scenario.get(group, [])
                 for member in train["members"]}
    requests = []
    for out in scenario["out"]:
        composition = sorted((u["typePrefix"], u["carriages"]) for u in out["trainUnits"])
        requests.append((int(out["departure"]), composition))
    used = set()
    pinned = copy.deepcopy(actions)
    for action in sorted(pinned, key=lambda a: a["startTime"]):
        if action["taskType"].get("predefined") != "Exit":
            continue
        composition = sorted(unit_type[m] for m in action["shuntingUnit"]["memberIDs"])
        candidates = []
        for index, (due, request_composition) in enumerate(requests):
            if request_composition == composition and index not in used:
                candidates.append((abs(due - action["startTime"]), due, index))
        if not candidates:
            continue
        _, due, index = min(candidates)
        used.add(index)
        action["startTime"] = action["endTime"] = due
    return pinned


def main():
    location = json.load(open(LOCATION))
    summary = collections.Counter()
    print(f"{'scenario':20} {'TORS':9} {'GAP=1':11} {'GAP=0':11} {'GAP=0 noWait':13} pinnedExits")
    for directory in sorted(glob.glob(os.path.join(RESULTS, "custom_*"))):
        name = os.path.basename(directory)
        plan_file = os.path.join(directory, "planning", "plan.json")
        eval_file = os.path.join(directory, "planning", "eval_result.json")
        if not os.path.exists(plan_file):
            continue
        actions = json.load(open(plan_file))["actions"]
        scenario = json.load(open(os.path.join(directory, f"scenario_{name}.json")))
        accepted = os.path.exists(eval_file) and json.load(open(eval_file)).get("solved")
        verdict = "accepted" if accepted else "rejected"
        statuses = [solve(actions, scenario, location, 1, False),
                    solve(actions, scenario, location, 0, False),
                    solve(actions, scenario, location, 0, True)]
        pinned = "-"
        if not accepted:
            pinned = solve(pin_exits_to_requests(actions, scenario), scenario, location, 0, True)
        print(f"{name:20} {verdict:9} {statuses[0]:11} {statuses[1]:11} {statuses[2]:13} {pinned}")
        summary[(verdict, *statuses, pinned)] += 1
    print("\nsummary (count: TORS, GAP=1, GAP=0, GAP=0 noWait, pinnedExits)")
    for key, count in summary.most_common():
        print(f"  {count:3}: {', '.join(key)}")


if __name__ == "__main__":
    main()
