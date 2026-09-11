import numpy as np

RISK_FILE = r"D:\FloodSafe\data\routing\uttarakhand\road_flood_risk.npz"
COORD_FILE = r"D:\FloodSafe\data\routing\uttarakhand\coordinates.npy"

r = np.load(RISK_FILE)
c = np.load(COORD_FILE)

indptr = r["indptr"]
indices = r["indices"]
distance = r["data"]
risk = r["risk"]

print("Searching for junctions with BOTH risky and safe outgoing roads...")

count = 0

for u in range(len(indptr) - 1):

    start = indptr[u]
    end = indptr[u + 1]

    if end - start <= 1:
        continue

    outgoing_risks = risk[start:end]

    has_extreme = np.any(outgoing_risks == 8)
    has_safe = np.any(outgoing_risks < 8)

    if not (has_extreme and has_safe):
        continue

    print()
    print("NODE:", u)
    print("COORD:", c[u])
    print("OUTGOING ROADS:")

    for j in range(start, end):

        v = int(indices[j])

        print(
            "  ->",
            v,
            "coord=", c[v],
            "distance=", round(float(distance[j]), 2),
            "risk=", float(risk[j])
        )

    count += 1

    if count >= 10:
        break

print()
print("Found:", count)