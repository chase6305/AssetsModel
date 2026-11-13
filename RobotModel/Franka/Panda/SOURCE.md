# Franka Panda

Seven-axis original Panda arm and two-finger Franka Hand. This is a separate
model from the existing Franka FR3 assets; no FR3 files are replaced.

| Entry | Assembly |
| --- | --- |
| `robot.urdf` | Arm and hand, including the original TCP |
| `arm.urdf` | Arm only: `panda_link0` to `panda_link8` |
| `hand.urdf` | Independent hand rooted at `panda_hand` |
| `home.json` | Legal display pose; second finger follows its original mimic |

Upstream: [frankarobotics/franka_ros](https://github.com/frankarobotics/franka_ros/tree/ddd2fffd9de44b02ad15b4bbb2bfa2cec4d60d98/franka_description),
revision `ddd2fffd9de44b02ad15b4bbb2bfa2cec4d60d98`.
Apache-2.0 [LICENSE](LICENSE) and [NOTICE](NOTICE) are retained.

Inertias are the upstream literature identification results attributed to Gaz
et al., not official manufacturer measurements. The archived `inertial.yaml`
retains that warning. Detailed environment collisions use the source Gazebo
configuration; finger collision primitives and empty auxiliary self-collision
frames are unchanged. The coordinate-zero pose is outside the source J4 limit;
use `home.json` for a legal display pose.

The existing refined Panda GLB palette, normals and geometry are copied exactly.
See the archived original `SOURCE.md` for material decisions and source caveats.

## Migration and reproduction

Moved from [`HumanoidAssets/FrankaPanda`](https://github.com/chase6305/HumanoidAssets/tree/acdeaeb086b1eafc16dac838b5ba88f2212b7d2a/FrankaPanda).
Only mesh filenames change in the URDFs. Visuals live in `Visual/glb/`;
collision surfaces are converted without decimation to `Collision/glb/`, and
original collision STL bytes remain in `Collision/stl/`. Primitive collisions
are unchanged. Link/joint frames, axes, limits, mimic rules and dynamics retain
their original values.

`source.tar.gz` stores the original source notes, DAE/Xacro/configuration inputs,
portable URDFs and import/validation manifests with original bytes. Extract it
outside the runtime model directory when inspecting source data. Its original
notes describe the historical HumanoidAssets layout; use this file for current
paths. Archived report references are historical evidence, not current checks.
`provenance.json` records the fixed migration revision, archive members, file
hashes, resource mapping and per-corner collision-conversion checks.

To regenerate the collision GLBs, run from the model directory with an
AssetsModel checkout available:

```bash
python /path/to/AssetsModel/scripts/convert_glb.py --files Collision/stl/*.stl --collision --output Collision/glb
```

Visual GLBs and the preview are byte-identical to the fixed migration source.
`validation.json` records checks of the migrated descriptions and runtime meshes;
these do not certify contact dynamics or physical controller accuracy.
