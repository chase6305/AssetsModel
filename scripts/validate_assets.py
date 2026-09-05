"""Validate portable URDF assets and generate a catalog using only the stdlib."""

from __future__ import annotations

import argparse
import json
import math
import struct
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PureWindowsPath
from glb_format import validate_glb

CATEGORIES = ("CameraModel", "RobotModel", "ToolModel")
JOINT_TYPES = {"fixed", "revolute", "continuous", "prismatic", "floating", "planar"}


def has_cycle(edges):
    # Iterative traversal also handles very long chains without recursion limits.
    done = set()
    for start in edges:
        active, stack = set(), [(start, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                active.discard(node)
                done.add(node)
            elif node in active:
                return True
            elif node not in done:
                active.add(node)
                stack.append((node, True))
                stack.extend((child, False) for child in edges.get(node, []))
    return False


def validate_urdf(path, glb_only=False):
    errors, warnings = [], []
    try:
        robot = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        return {"errors": [f"XML: {exc}"], "warnings": [], "name": None}
    if robot.tag != "robot" or not robot.get("name", "").strip():
        errors.append("Expected a named <robot> root")
    links, joints = robot.findall("link"), robot.findall("joint")
    for kind, elements in [("link", links), ("joint", joints)]:
        names = [x.get("name", "") for x in elements]
        if any(not n.strip() for n in names):
            errors.append(f"Unnamed {kind}")
        errors.extend(f"Duplicate {kind}: {name}" for name, count in Counter(names).items() if count > 1)
    link_names = {x.get("name") for x in links}
    joint_names = {x.get("name") for x in joints}
    children, edges, mimics = [], {}, {}

    def numbers(element, attribute, count, required=False):
        value = element.get(attribute)
        if value is None:
            if required:
                errors.append(f"<{element.tag}> missing {attribute}")
            return None
        try:
            values = [float(x) for x in value.split()]
            if len(values) != count or not all(math.isfinite(x) for x in values):
                raise ValueError
            return values
        except ValueError:
            errors.append(f"<{element.tag}> invalid {attribute}: {value!r}")
            return None

    for element in robot.iter():
        if element.tag == "origin":
            numbers(element, "xyz", 3)
            numbers(element, "rpy", 3)
        elif element.tag == "axis":
            axis = numbers(element, "xyz", 3, True)
            if axis is not None and sum(x * x for x in axis) == 0:
                errors.append("Joint axis is zero")
        elif element.tag == "color":
            rgba = numbers(element, "rgba", 4, True)
            if rgba is not None and any(x < 0 or x > 1 for x in rgba):
                errors.append("Color components must be in [0, 1]")
        elif element.tag in ("box", "sphere", "cylinder"):
            fields = {"box": [("size", 3)], "sphere": [("radius", 1)],
                      "cylinder": [("radius", 1), ("length", 1)]}[element.tag]
            for field, count in fields:
                values = numbers(element, field, count, True)
                if values is not None and any(x <= 0 for x in values):
                    errors.append(f"<{element.tag}> {field} must be positive")
        elif element.tag == "mesh":
            scale = numbers(element, "scale", 3)
            if scale is not None and any(x == 0 for x in scale):
                errors.append("Mesh scale cannot be zero")

    resources = set()
    for resource in robot.findall(".//mesh") + robot.findall(".//texture"):
        filename = resource.get("filename", "")
        relative = Path(filename)
        if glb_only and resource.tag == "mesh" and (relative.suffix != ".glb" or relative.parent.name != "glb"):
            errors.append(f"Mesh reference must use a glb/ format directory: {filename}")
        if (not filename or ":" in filename or "\\" in filename or relative.is_absolute()
                or PureWindowsPath(filename).is_absolute()):
            errors.append(f"Resource must use a relative portable path: {filename!r}")
            continue
        resolved = (path.parent / relative).resolve()
        if not resolved.is_relative_to(path.parent.resolve()):
            errors.append(f"Resource escapes the model directory: {filename}")
        elif not resolved.is_file():
            errors.append(f"Missing resource: {filename}")
        elif resolved.stat().st_size == 0:
            errors.append(f"Empty resource: {filename}")
        resources.add(filename)

    for joint in joints:
        name, kind = joint.get("name"), joint.get("type")
        if kind not in JOINT_TYPES:
            errors.append(f"{name}: invalid joint type {kind!r}")
        parent, child = joint.find("parent"), joint.find("child")
        if parent is None or child is None:
            errors.append(f"{name}: missing parent or child")
            continue
        a, b = parent.get("link"), child.get("link")
        if a not in link_names or b not in link_names:
            errors.append(f"{name}: references unknown link")
        children.append(b)
        edges.setdefault(a, []).append(b)
        limit = joint.find("limit")
        if kind in ("revolute", "prismatic") and limit is None:
            errors.append(f"{name}: missing joint limit")
        if limit is not None:
            lower = numbers(limit, "lower", 1, kind in ("revolute", "prismatic"))
            upper = numbers(limit, "upper", 1, kind in ("revolute", "prismatic"))
            if lower is not None and upper is not None and lower[0] > upper[0]:
                errors.append(f"{name}: lower limit exceeds upper limit")
            for field in ("effort", "velocity"):
                value = numbers(limit, field, 1, True)
                if value is not None and value[0] < 0:
                    errors.append(f"{name}: negative {field}")
                elif value is not None and value[0] == 0 and kind != "fixed":
                    warnings.append(f"{name}: zero {field}; review before dynamic simulation")
        mimic = joint.find("mimic")
        if mimic is not None:
            target = mimic.get("joint")
            if target not in joint_names:
                errors.append(f"{name}: unknown mimic joint {target!r}")
            mimics[name] = [target]
            numbers(mimic, "multiplier", 1)
            numbers(mimic, "offset", 1)
    roots = sorted(link_names - set(children))
    if len(roots) != 1:
        errors.append(f"Expected one root link, found {len(roots)}: {roots}")
    errors.extend(f"Link has multiple parents: {name}" for name, count in Counter(children).items() if count > 1)
    if has_cycle(edges):
        errors.append("Link graph contains a cycle")
    if has_cycle(mimics):
        errors.append("Mimic graph contains a cycle")
    missing_inertia = []
    for link in links:
        inertial = link.find("inertial")
        if inertial is None:
            if link.find("visual") is not None or link.find("collision") is not None:
                missing_inertia.append(link.get("name"))
            continue
        mass, inertia = inertial.find("mass"), inertial.find("inertia")
        if mass is None or inertia is None:
            errors.append(f"{link.get('name')}: incomplete inertia")
            continue
        value = numbers(mass, "value", 1, True)
        if value is not None and value[0] <= 0:
            errors.append(f"{link.get('name')}: mass must be positive")
        entries = [numbers(inertia, key, 1, True) for key in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")]
        if all(x is not None for x in entries):
            a, b, c, d, e, f = (x[0] for x in entries)
            if a <= 0 or a*d-b*b <= 0 or a*(d*f-e*e)-b*(b*f-c*e)+c*(b*e-c*d) <= 0:
                errors.append(f"{link.get('name')}: inertia is not positive definite")
    if missing_inertia:
        warnings.append(f"Geometry links without inertia: {', '.join(missing_inertia)}")
    return {"name": robot.get("name"), "links": len(links), "joints": len(joints),
            "actuated_joints": sum(x.get("type") in ("revolute", "continuous", "prismatic")
                                   and x.find("mimic") is None for x in joints),
            "root_links": roots, "resources": sorted(resources),
            "errors": errors, "warnings": warnings}


def check_mesh(path):
    """Check mesh containers without installing a geometry stack."""
    if path.suffix.lower() == ".glb":
        try:
            return validate_glb(path)["faces"], []
        except (ValueError, KeyError, IndexError, OSError, struct.error) as exc:
            return 0, [str(exc)]
    if path.suffix.lower() == ".stl":
        size = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(84)
        if len(header) == 84 and size == 84 + 50 * struct.unpack_from("<I", header, 80)[0]:
            count = struct.unpack_from("<I", header, 80)[0]
            return count, [] if count else ["Empty STL"]
        return 0, ["Invalid binary STL length (ASCII STL is not supported by this check)"]
    try:
        root = ET.parse(path).getroot()
        namespace = "{http://www.collada.org/2005/11/COLLADASchema}"
        if root.tag != namespace + "COLLADA":
            return 0, ["Expected COLLADA root"]
        faces = sum(int(x.get("count", 0)) for x in root.iter()
                    if x.tag in (namespace + "triangles", namespace + "polylist"))
        return faces, [] if faces else ["No mesh triangles or polygons"]
    except (ET.ParseError, OSError, ValueError) as exc:
        return 0, [str(exc)]


def layout_errors(path):
    if path.suffix.lower() == ".dae":
        return ["DAE source files must be converted to GLB before adding them to the library"]
    if path.suffix.lower() in (".stl", ".glb") and path.parent.name != path.suffix.lower()[1:]:
        return [f"Mesh must be inside its {path.suffix.lower()[1:]}/ format directory"]
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--meshes", action="store_true", help="Also check every STL/DAE/GLB container")
    parser.add_argument("--layout", action="store_true", help="Enforce GLB URDF references, format directories and no DAE sources")
    parser.add_argument("--json", type=Path, help="Write the full machine-readable catalog")
    parser.add_argument("--markdown", type=Path, help="Write a linked model catalog")
    parser.add_argument("--strict", action="store_true", help="Treat simulation-readiness warnings as failures")
    args = parser.parse_args()
    root = args.root.resolve()
    models, meshes = [], []
    for category in CATEGORIES:
        for path in sorted((root / category).rglob("*.urdf")):
            relative = path.relative_to(root).as_posix()
            models.append({"path": relative, "category": category, **validate_urdf(path, glb_only=args.layout)})
    if not models:
        parser.error("No URDF assets found")
    errors = sum(len(x["errors"]) for x in models)
    warnings = sum(len(x["warnings"]) for x in models)
    if args.meshes or args.layout:
        for category in CATEGORIES:
            for path in sorted((root / category).rglob("*")):
                if path.suffix.lower() in (".stl", ".dae", ".glb"):
                    faces, issues = check_mesh(path) if args.meshes else (0, [])
                    if args.layout:
                        issues.extend(layout_errors(path))
                    meshes.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
                                   "faces": faces, "errors": issues})
                    errors += len(issues)
    report = {"summary": {"models": len(models), "meshes_checked": len(meshes),
                          "errors": errors, "warnings": warnings}, "models": models, "meshes": meshes}
    for item in models + meshes:
        for error in item["errors"]:
            print(f"ERROR {item['path']}: {error}")
    print(f"{len(models)} URDFs, {len(meshes)} meshes: {errors} errors, {warnings} warnings")
    if warnings:
        print("Warnings describe missing inertia / zero limits; use --json to inspect details.")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n")
    if args.markdown:
        import os
        from urllib.parse import quote
        lines = ["# Model catalog", "", "Generated by `scripts/validate_assets.py`. Actuated joints exclude mimic joints.", "",
                 "| Model | Category | Links | Actuated joints | URDF |", "| --- | --- | ---: | ---: | --- |"]
        for item in models:
            href = quote(os.path.relpath(root / item["path"], args.markdown.resolve().parent).replace(os.sep, "/"))
            lines.append(f"| {item['name']} | {item['category']} | {item.get('links', 0)} | {item.get('actuated_joints', 0)} | [{Path(item['path']).stem}]({href}) |")
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text("\n".join(lines) + "\n")
    return int(errors > 0 or (args.strict and warnings > 0))


if __name__ == "__main__":
    raise SystemExit(main())
