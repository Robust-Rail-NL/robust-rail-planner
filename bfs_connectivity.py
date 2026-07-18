import json
from collections import deque

LOCATION_FILE = r"C:\Users\Tycho\Desktop\SchoolTU\Year4\q4\Robust-Rail-NL\scenario-planning-inputs\Location_KleineBinckhorst\location_solver.json"


def load_graph(path):
    with open(path) as f:
        data = json.load(f)

    a_adj = {}
    b_adj = {}
    for tp in data["trackParts"]:
        tid = tp["id"]
        a_adj.setdefault(tid, set())
        b_adj.setdefault(tid, set())
        for neighbor in tp.get("aSide", []):
            a_adj[tid].add(neighbor)
        for neighbor in tp.get("bSide", []):
            b_adj[tid].add(neighbor)

    return a_adj, b_adj


def bfs(adj, start, goal):
    if start == goal:
        return [start]
    visited = {start}
    queue = deque([(start, [start])])
    while queue:
        node, path = queue.popleft()
        for neighbor in adj.get(node, set()):
            if neighbor == goal:
                return path + [neighbor]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))
    return None


def main():
    a_adj, b_adj = load_graph(LOCATION_FILE)

    track_ids = sorted({k for k in a_adj}, key=int)
    print("Available track IDs:", ", ".join(track_ids))
    id1 = input("Track ID 1: ").strip()
    id2 = input("Track ID 2: ").strip()

    if id1 not in a_adj:
        print(f"Track {id1} not found."); return
    if id2 not in a_adj:
        print(f"Track {id2} not found."); return

    a_path = bfs(a_adj, id1, id2)
    b_path = bfs(b_adj, id1, id2)

    print()
    if a_path:
        print(f"A-side connected: True")
        print(f"  Path: {' -> '.join(a_path)}")
    else:
        print("A-side connected: False")

    if b_path:
        print(f"B-side connected: True")
        print(f"  Path: {' -> '.join(b_path)}")
    else:
        print("B-side connected: False")


if __name__ == "__main__":
    main()
