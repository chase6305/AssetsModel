"""Load all URDF poses, optionally comparing against a pre-change asset tree."""

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import yourdfpy
from lxml import etree as ET


def description(path):
    tree = ET.parse(str(path))
    for mesh in tree.findall(".//mesh"):
        mesh.set("filename", "RESOURCE")
    # Ignore formatting but keep every non-resource URDF attribute.
    return [(element.tag, dict(element.attrib)) for element in tree.iter() if isinstance(element.tag, str)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Optional reference tree for geometry/kinematics comparison")
    parser.add_argument("--assets", "--glb", dest="assets", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    results = []
    baseline = args.source or args.assets
    if args.source:
        source_paths = {p.relative_to(args.source) for category in ("CameraModel", "RobotModel", "ToolModel")
                        for p in (args.source / category).rglob("*.urdf")}
        target_paths = {p.relative_to(args.assets) for category in ("CameraModel", "RobotModel", "ToolModel")
                        for p in (args.assets / category).rglob("*.urdf")}
        if source_paths != target_paths:
            parser.error("Reference and target URDF sets differ")
    for category in ("CameraModel", "RobotModel", "ToolModel"):
        for original_path in sorted((baseline / category).rglob("*.urdf")):
            relative = original_path.relative_to(baseline)
            target = args.assets / relative
            result = {"path": relative.as_posix()}
            try:
                if args.source and description(original_path) != description(target):
                    raise ValueError("URDF structure or non-resource attributes changed")
                models = [yourdfpy.URDF.load(str(path), build_collision_scene_graph=True,
                                             load_collision_meshes=True) for path in
                          ((original_path, target) if args.source else (target,))]
                configs = [models[0].zero_cfg]
                for fraction in (0.25, 0.75):
                    configs.append(np.array([
                        joint.limit.lower * (1 - fraction) + joint.limit.upper * fraction
                        if joint.limit is not None and joint.limit.lower is not None and joint.limit.upper is not None
                        else 0.0 for joint in models[0].actuated_joints
                    ]))
                max_bounds_error = 0.0
                for config in configs:
                    for model in models:
                        model.update_cfg(config)
                    for link in models[0].link_map:
                        matrices = [model.scene.graph.get(link)[0] for model in models]
                        if not all(np.isfinite(matrix).all() for matrix in matrices):
                            raise ValueError(f"Non-finite link transform: {link}")
                        if args.source:
                            np.testing.assert_allclose(matrices[0], matrices[1], atol=1e-12, rtol=0)
                    for kind in ("scene", "collision_scene"):
                        scenes = [getattr(model, kind) for model in models]
                        # Surface face count must survive mesh loading/assembly.
                        counts = [sum(len(s.geometry[name].faces) for _, name in
                                      [s.graph[node] for node in s.graph.nodes_geometry]
                                      if hasattr(s.geometry[name], "faces")) for s in scenes]
                        if any(count <= 0 for count in counts):
                            raise ValueError(f"Empty {kind}")
                        if not all(s.bounds is not None and np.isfinite(s.bounds).all() for s in scenes):
                            raise ValueError(f"Invalid {kind} bounds")
                        if args.source and counts[0] != counts[1]:
                            raise ValueError(f"{kind} triangle count changed: {counts}")
                        if args.source:
                            delta = float(np.max(np.abs(scenes[0].bounds - scenes[1].bounds)))
                            max_bounds_error = max(max_bounds_error, delta)
                            np.testing.assert_allclose(scenes[0].bounds, scenes[1].bounds, atol=1e-6, rtol=0)
                result.update(status="passed", poses=3, actuated_joints=models[0].num_actuated_joints)
                if args.source:
                    result["max_bounds_error_m"] = max_bounds_error
            except Exception as exc:
                result.update(status="error", error=str(exc))
                print(f"ERROR {relative}: {exc}", flush=True)
            results.append(result)
    summary = {"urdfs": len(results), "poses": sum(x.get("poses", 0) for x in results),
               "errors": sum(x["status"] == "error" for x in results),
               "mode": "comparison" if args.source else "load",
               "yourdfpy": yourdfpy.__version__}
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps({"summary": summary, "models": results}, indent=2) + "\n")
    print(json.dumps(summary))
    return int(summary["errors"] > 0 or not results)


if __name__ == "__main__":
    raise SystemExit(main())
