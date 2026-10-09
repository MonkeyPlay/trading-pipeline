# p1_pool_tuning_v1

Definition `a695e53c1a7d4f1b` (contracts/p1_pool_tuning.py, docs/p1_pool_tuning_v1_definition.json), fixed and committed before this run. **Development data** (278 sessions): a candidate at most, never a result.

**Decision (primary, direction_15m, tuned minus A):** no sufficiently reliable improvement over the frequencies was established on development data.

Mean multiclass Brier (lower is better) over the walk-forward-tuned sessions; differences are paired per session with a 95 % block-bootstrap interval.

| target | sessions | A (frequencies) | B (K=5, k=5) | tuned | tuned - A | tuned - B | B - A | infinite log loss A / B / tuned | cells chosen (most often) |
|---|---:|---:|---:|---:|---|---|---|---|---|
| `first_move_5m` | 57 | 0.5048 | 0.4644 | 0.4719 | -0.0328 [-0.0712, +0.0145] | +0.0076 [+0.0000, +0.0189] | -0.0404 [-0.0833, +0.0104] | 0 / 0 / 0 | K=5, k=5 (48), K=5, k=10 (8) |
| `opening_type_15m` | 216 | 0.4875 | 0.5102 | 0.4935 | +0.0060 [-0.0041, +0.0164] | -0.0167 [-0.0332, -0.0002] | +0.0227 [+0.0039, +0.0421] | 0 / 0 / 0 | K=40, k=20 (117), K=5, k=20 (24) |
| `direction_15m` | 217 | 0.6103 | 0.6206 | 0.6117 | +0.0014 [-0.0143, +0.0170] | -0.0090 [-0.0297, +0.0120] | +0.0104 [-0.0163, +0.0366] | 0 / 0 / 0 | K=10, k=20 (151), K=10, k=10 (27) |
| `opening_bias_30m` | 217 | 0.5779 | 0.6138 | 0.5903 | +0.0124 [+0.0004, +0.0248] | -0.0235 [-0.0428, -0.0038] | +0.0359 [+0.0108, +0.0606] | 0 / 0 / 0 | K=10, k=20 (181), K=5, k=20 (21) |
| `close_direction_rth` | 196 | 0.5603 | 0.5911 | 0.5649 | +0.0046 [-0.0095, +0.0182] | -0.0262 [-0.0491, -0.0031] | +0.0308 [+0.0042, +0.0579] | 0 / 0 / 0 | K=40, k=20 (154), K=40, k=2 (27) |
| `session_type_rth` | 109 | 0.7535 | 0.7540 | 0.7566 | +0.0031 [-0.0127, +0.0189] | +0.0026 [-0.0304, +0.0366] | +0.0005 [-0.0313, +0.0314] | 0 / 0 / 0 | K=40, k=20 (59), K=5, k=20 (22) |
| `first_level_tested` | 154 | 0.8665 | 0.8219 | 0.8192 | -0.0473 [-0.0785, -0.0161] | -0.0027 [-0.0216, +0.0152] | -0.0446 [-0.0746, -0.0150] | 0 / 0 / 0 | K=5, k=5 (64), K=20, k=5 (49) |
