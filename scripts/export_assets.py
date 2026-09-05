"""Copy selected models and GLB resources to a fresh, self-contained directory."""

import argparse
import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from validate_assets import validate_urdf


def export_assets(root, models, output, referenced_only=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    folders = [(root / model).resolve() for model in models]
    if not folders or any(not folder.is_dir() or not folder.is_relative_to(root) for folder in folders):
        raise ValueError("Model directories must exist inside the asset root")
    if any(output.is_relative_to(folder) or folder.is_relative_to(output) for folder in folders):
        raise ValueError("Output must not overlap a source model directory")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Export requires an empty output directory")
    urdfs = {p for folder in folders for p in folder.rglob("*.urdf")}
    if not urdfs:
        raise ValueError("No URDF files found in selected models")
    files = set(urdfs)
    for urdf in sorted(urdfs):
        result = validate_urdf(urdf, glb_only=True)
        if result["errors"]:
            raise ValueError(f"{urdf}: {result['errors']}")
        tree = ET.parse(urdf)
        for resource in tree.findall(".//mesh") + tree.findall(".//texture"):
            files.add((urdf.parent / resource.get("filename")).resolve())
    if not referenced_only:
        files.update(p for folder in folders for p in folder.rglob("*.glb"))
    if any(not p.is_file() or not p.resolve().is_relative_to(root) for p in files):
        raise ValueError("A resource is missing or escapes the asset root")
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for source in sorted(files):
        relative = source.relative_to(root)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        records.append({"path": relative.as_posix(), "bytes": target.stat().st_size,
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    report = {"summary": {"urdfs": len(urdfs), "glbs": sum(p.suffix == ".glb" for p in files),
                           "bytes": sum(x["bytes"] for x in records)}, "files": records}
    (output / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--models", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--referenced-only", action="store_true", help="Omit GLBs not referenced by the selected URDFs")
    args = parser.parse_args()
    try:
        report = export_assets(args.root, args.models, args.output, args.referenced_only)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()
