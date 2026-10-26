# 011: Expectations first, and a small seed benchmark

**Decision:** Every benchmark entry must carry an `expected_outcome`, and the results store the SHA-256 of the benchmark file. Three tiny synthetic repos ship with the project so the harness runs end to end today. Real public-repo issues are added later.

**Why:** Writing expectations after seeing results makes the numbers meaningless. A hash plus a committed file lets anyone check. Synthetic seeds let the harness be tested and demonstrated without network or a model.

**Cost:** Seed issues are easy, so their pass rates say little about real repos. The headline model comparison must use real issues.
