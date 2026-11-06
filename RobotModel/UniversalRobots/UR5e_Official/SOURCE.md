# Universal Robots UR5e — official description variant

Entry: `robot.urdf`, six independent revolute joints, original `flange`, `tool0`
and `ft_frame` frames. `home.json` supplies the legal pose used in `preview.png`.

This variant supplements `../UR5e/UR5e.urdf`. It preserves the official ROS2
description's names, frame tree and physical values; the existing AssetsModel
UR5e has different names and parameters and remains unchanged. The two URDFs
are separate entry points and are not interchangeable by link/joint name.

Upstream: [UniversalRobots/Universal_Robots_ROS2_Description](https://github.com/UniversalRobots/Universal_Robots_ROS2_Description/tree/3c173361e155231e5376d2682ddf920d3d79c112),
revision `3c173361e155231e5376d2682ddf920d3d79c112` (humble).
The upstream BSD terms are retained in [LICENSE](LICENSE).

The source macro used `ur_type=ur5e`, empty prefix, `parent=world` and disabled
ros2_control tags. The nominal YAML is not a physical arm's individual kinematic
calibration. Original mass, COM, inertia and limits are unchanged. Existing
silver/grey, blue and black GLB materials and refined normals are copied exactly.
Rendering finishes are visual estimates, not measured reflectance data.

## Migration and reproduction

Moved from [`HumanoidAssets/UniversalRobotsUR5e`](https://github.com/chase6305/HumanoidAssets/tree/acdeaeb086b1eafc16dac838b5ba88f2212b7d2a/UniversalRobotsUR5e).
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
