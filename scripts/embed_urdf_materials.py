"""Embed unambiguous URDF colors into otherwise unmaterialed visual GLBs.

Dry run by default. Existing GLB materials and files shared with collision
geometry are retained. No color is guessed when URDF appearances disagree.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

from glb_format import read_glb, validate_glb

CATEGORIES = ("CameraModel", "RobotModel", "ToolModel")


def encode(tree, binary):
    raw = json.dumps(tree, separators=(",", ":"), allow_nan=False).encode()
    raw += b" " * (-len(raw) % 4)
    binary += b"\0" * (-len(binary) % 4)
    length = 28 + len(raw) + len(binary)
    return (struct.pack("<III", 0x46546C67, 2, length)
            + struct.pack("<II", len(raw), 0x4E4F534A) + raw
            + struct.pack("<II", len(binary), 0x004E4942) + binary)


def references(root):
    colors, roles = defaultdict(set), defaultdict(set)
    for category in CATEGORIES:
        for path in sorted((root / category).rglob("*.urdf")):
            robot = ET.parse(path).getroot()
            named = {m.get("name"): m for m in robot.findall("material")}
            for role in ("visual", "collision"):
                for element in robot.findall(f"./link/{role}"):
                    mesh = element.find("geometry/mesh")
                    if mesh is None:
                        continue
                    target = (path.parent / mesh.get("filename", "")).resolve()
                    if not target.is_relative_to(root):
                        raise ValueError(f"Resource outside asset root: {target}")
                    roles[target].add(role)
                    if role == "visual":
                        material = element.find("material")
                        color = material.find("color") if material is not None else None
                        if color is None and material is not None and material.get("name") in named:
                            color = named[material.get("name")].find("color")
                        rgba = tuple(float(v) for v in color.get("rgba", "").split()) if color is not None else None
                        colors[target].add(rgba)
    return colors, roles


def embed(data, colors):
    """Return a new container only if every visual instance has the same color."""
    tree, binary = read_glb(data)
    if tree.get("materials"):
        return None, "existing materials retained"
    if len(colors) != 1 or None in colors:
        return None, "missing or conflicting URDF colors"
    color = next(iter(colors))
    if len(color) != 4 or any(not 0 <= v <= 1 for v in color):
        raise ValueError("URDF color must contain four finite values in [0, 1]")
    material = {"name": "URDF color", "pbrMetallicRoughness": {
        "baseColorFactor": list(color), "metallicFactor": 0.0, "roughnessFactor": 1.0}}
    if color[3] < 1:
        material["alphaMode"] = "BLEND"
    changed = False
    for mesh in tree.get("meshes", []):
        for primitive in mesh["primitives"]:
            if primitive.get("mode", 4) == 4:
                if "material" in primitive:
                    raise ValueError("Primitive references missing material")
                primitive["material"] = 0
                changed = True
    if not changed:
        return None, "no triangles"
    tree["materials"] = [material]
    payload = encode(tree, binary)
    validate_glb(payload)
    # The complete binary payload is retained, including normals and CAD guides.
    assert read_glb(payload)[1] == binary
    return payload, "embedded unique URDF color"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--exclude", action="append", default=[], help="Repository-relative directory to exclude")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    root = args.root.resolve()
    if args.apply and args.backup_dir.resolve().is_relative_to(root):
        parser.error("Keep backups outside the asset tree")
    colors, roles = references(root)
    rows = []
    for path in sorted(colors):
        relative = path.relative_to(root)
        if path.suffix.lower() != ".glb" or any(relative.is_relative_to(Path(p)) for p in args.exclude):
            continue
        data = path.read_bytes()
        if "collision" in roles[path]:
            payload, reason = None, "shared with collision; retained"
        else:
            payload, reason = embed(data, colors[path])
        entry = {"path": relative.as_posix(), "changed": payload is not None, "reason": reason,
                 "sha256_before": hashlib.sha256(data).hexdigest(),
                 "sha256_after": hashlib.sha256(payload or data).hexdigest()}
        if payload is not None and args.apply:
            backup = args.backup_dir.resolve() / relative
            if backup == path:
                raise ValueError("Backup resolves to source")
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists() and backup.read_bytes() != data:
                raise ValueError("Backup contains different data; choose a fresh directory")
            if not backup.exists():
                with backup.open("xb") as stream:
                    stream.write(data)
            staging = path.with_suffix(".glb.tmp")
            staging.write_bytes(payload)
            staging.replace(path)
        rows.append(entry)
    report = {"summary": {"visual_glbs": len(rows), "changed": sum(r["changed"] for r in rows), "applied": args.apply}, "files": rows}
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()
