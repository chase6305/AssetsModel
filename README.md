# AssetsModel

AssetsModel is a URDF asset repository for industrial robots, cameras, and end-effectors. The repository is organized by asset category, vendor, and model name so that each model can be referenced directly from simulation, planning, or visualization projects.

## Repository Layout

```text
AssetsModel/
├── CameraModel/
│   └── <Vendor>/<Model>/
├── RobotModel/
│   └── <Vendor>/<Model>/
└── ToolModel/
    └── <Model>/
```

### CameraModel

Camera assets are grouped by vendor and model. A typical camera directory contains:

- one or more URDF files
- a meshes directory for geometry assets
- optional Graphviz source files and exported PDF diagrams

Examples:

- `CameraModel/RealSense/RealSense_D405/RealSense_D405.urdf`
- `CameraModel/RealSense/RealSense_D405/RealSense_D405_and_plug.urdf`

### RobotModel

Robot assets are grouped by brand and robot model. A typical robot directory contains:

- a main URDF file
- a `Visual/` directory for visual meshes
- a `Collision/` directory for collision meshes
- optional preview images

Examples:

- `RobotModel/ABB/IRB1200_5_90/IRB1200_5_90.urdf`
- `RobotModel/UniversalRobots/UR5e/UR5e.urdf`

### ToolModel

Tool assets contain URDF descriptions and supporting meshes for grippers or other end-effectors.

Example:

- `ToolModel/Robotiq2F85/Robotiq2F85.urdf`

## File Conventions

- Each model usually uses the directory name as the URDF base name.
- Mesh paths in URDF files are stored as relative paths so the model folder can be moved or reused as a self-contained unit.
- Robot models commonly separate visual and collision geometry into different directories.
- Some camera models provide multiple URDF variants, such as a base model and a version with an added plug.

## How To Use

Reference the URDF file you need directly from your simulator or robotics application.

Example paths:

```text
RobotModel/ABB/IRB1200_5_90/IRB1200_5_90.urdf
CameraModel/RealSense/RealSense_D405/RealSense_D405.urdf
ToolModel/Robotiq2F85/Robotiq2F85.urdf
```

Because mesh files are referenced relatively, keep the URDF file and its sibling asset directories together when copying a model into another workspace.

## Recommended Integration Workflow

1. Select the required robot, camera, or tool model.
2. Load the corresponding URDF into your robotics framework.
3. Verify that all referenced mesh files are available at their original relative paths.
4. If needed, combine robot, camera, and tool models in a higher-level assembly package.

## Contribution Notes

When adding a new asset:

1. Place it under the correct top-level category.
2. Follow the existing vendor and model naming pattern.
3. Keep URDF filenames aligned with the model directory name when possible.
4. Store mesh resources next to the URDF using relative paths.
5. Separate visual and collision meshes when the asset type requires both.

## Current Scope

The repository currently includes:

- industrial robot models from multiple vendors
- RealSense camera models and variants
- end-effector assets such as the Robotiq 2F85 gripper

This repository is intended to act as a reusable asset library for robotics development, offline programming, simulation, and visualization.
