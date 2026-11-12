"""Reproduce the reviewed 244-corner FANUC M20 Link5 normal refinement.

The historical report supplies an explicit mask and exact output-normal hash.
This tool discovers no new planar regions and changes no other normal corners.
Use --output-model for a review copy; --apply additionally requires an external
backup directory. Run geometry work under the shared resource limits/lock.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from verify_camera_finishes import (check_m20_normal_update, report_destination,
                                    reviewed_m20_mask)

ROOT = Path(__file__).resolve().parents[1]
MODEL = Path("RobotModel/Fanuc/M_20iA")
VISUAL = Path("Visual/glb/Link5.glb")
SOURCE_SHA = "2c6bdb161588c77f2eeff03faa07c54a41a3992629049efe37e6cb506cb15f4c"
TARGET_SHA = "235663deae306b8cedbf458024b3c2fba0cc83ce9ef6d229de30f060665a4007"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def repair(source, root=ROOT):
    """Rebuild only the exact historical normal field and verify emitted bytes."""
    import numpy as np
    from glb_format import read_glb, validate_glb
    from prepare_preview import accessor_array
    from refine_materials import encode_glb
    from refine_visual_normals import prune_accessors
    from smooth_oriented_sheets import smooth_oriented_normals

    if sha(source) != SOURCE_SHA:
        raise ValueError("Source SHA changed; this normal mask cannot be generalized")
    entry = reviewed_m20_mask(root)
    mask = entry["primitives"][0]
    tree, binary = read_glb(source)
    if len(tree["meshes"]) != 1 or len(tree["meshes"][0]["primitives"]) != 1:
        raise ValueError("Expected only the reviewed mesh0/primitive0")
    primitive = tree["meshes"][0]["primitives"][0]
    if (primitive.get("mode", 4) != 4 or set(primitive["attributes"]) != {"POSITION", "NORMAL"}
            or tree["materials"][primitive["material"]].get("name") != "RAL9005_003-material"):
        raise ValueError("Primitive or material differs from the reviewed scope")
    vertices = accessor_array(tree, binary, primitive["attributes"]["POSITION"])
    faces = accessor_array(tree, binary, primitive["indices"]).reshape(-1, 3)
    original = np.ascontiguousarray(
        accessor_array(tree, binary, primitive["attributes"]["NORMAL"])[faces].reshape(-1, 3), dtype="<f4")
    if len(original) != mask["corner_count"] or sha(original.tobytes()) != mask["source_corner_normals_sha256"]:
        raise ValueError("Ordered source NORMAL bytes differ from the reviewed mask")
    chosen = np.zeros(len(original), dtype=bool)
    chosen[mask["selected_corner_indices"]] = True
    normals = original.copy()
    normals[chosen] = smooth_oriented_normals(vertices, faces, angle=30)[chosen].astype("<f4")
    if (normals[~chosen].tobytes() != original[~chosen].tobytes()
            or sha(normals.tobytes()) != mask["expected_output_corner_normals_sha256"]):
        raise ValueError("The historical target normal field was not reproduced exactly")
    # Exact float32 POSITION/NORMAL tuple deduplication: no tolerance welding,
    # quantization, triangle reordering, or geometry simplification.
    tuples = np.ascontiguousarray(np.column_stack((vertices[faces].reshape(-1, 3), normals)), dtype="<f4")
    opaque = tuples.view(np.dtype((np.void, 24))).reshape(-1)
    _, first, inverse = np.unique(opaque, return_index=True, return_inverse=True)
    complete, new_faces = tuples[first], inverse.reshape(-1, 3)
    if complete[new_faces, :3].tobytes() != vertices[faces].tobytes():
        raise ValueError("Ordered triangle position bytes changed")
    target, packed = copy.deepcopy(tree), bytearray(binary)

    def append(values, component, kind, position=False):
        values = np.ascontiguousarray(values)
        packed.extend(b"\0" * (-len(packed) % 4))
        view = len(target["bufferViews"])
        target["bufferViews"].append({"buffer": 0, "byteOffset": len(packed), "byteLength": values.nbytes})
        packed.extend(values.tobytes())
        index = len(target["accessors"])
        accessor = {"bufferView": view, "componentType": component, "type": kind, "count": len(values)}
        if position:
            accessor.update(min=values.min(axis=0).tolist(), max=values.max(axis=0).tolist())
        target["accessors"].append(accessor)
        return index

    updated = target["meshes"][0]["primitives"][0]
    updated["attributes"]["POSITION"] = append(complete[:, :3], 5126, "VEC3", position=True)
    updated["attributes"]["NORMAL"] = append(complete[:, 3:], 5126, "VEC3")
    dtype, component = ("<u2", 5123) if len(complete) <= 65536 else ("<u4", 5125)
    updated["indices"] = append(new_faces.astype(dtype).reshape(-1), component, "SCALAR")
    target["buffers"][0]["byteLength"] = len(packed)
    packed.extend(b"\0" * (-len(packed) % 4))
    target, output_binary = prune_accessors(target, bytes(packed))
    output = encode_glb(target, output_binary)
    validate_glb(output)
    if sha(output) != TARGET_SHA:
        raise ValueError("Emitted GLB differs from the exact reviewed candidate")
    verification = check_m20_normal_update(root, source, output)
    return output, verification


def atomic_replace(path, payload):
    """Stage a complete replacement while retaining the source permission bits."""
    staged = None
    try:
        with tempfile.NamedTemporaryFile(prefix="." + path.name + ".", dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(staged, path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def run(root=ROOT, *, output_model=None, apply=False, backup_dir=None):
    root = Path(root).resolve()
    source_model = root / MODEL
    path = source_model / VISUAL
    if path.resolve() != path or not path.is_file():
        raise ValueError("Expected a regular source Link5 without symlinks")
    if bool(output_model) == bool(apply):
        raise ValueError("Choose exactly one of output_model or apply")
    backup = None
    if apply:
        if backup_dir is None:
            raise ValueError("Apply requires an external backup directory")
        backup = Path(backup_dir).resolve()
        if backup.is_relative_to(root) or root.is_relative_to(backup):
            raise ValueError("Backup must be outside the source asset tree")
    else:
        output_model = Path(output_model).resolve()
        if output_model.is_relative_to(root) or root.is_relative_to(output_model):
            raise ValueError("Review output must be outside the source tree")
        if output_model.exists() and (not output_model.is_dir() or any(output_model.iterdir())):
            raise ValueError("Review output must be new or empty")
        if any(p.is_symlink() for p in source_model.rglob("*")):
            raise ValueError("Review source must contain ordinary files without symlinks")
    original = path.read_bytes()
    report = {"source_model": str(source_model), "path": (MODEL / VISUAL).as_posix(),
              "source_sha256": sha(original), "target_sha256": TARGET_SHA, "selected_corners": 244}
    if sha(original) == TARGET_SHA:
        # Idempotent apply retains the exact accepted target and its mtime.
        if not apply:
            raise ValueError("Source is already refined; reproduce a review copy from the original baseline")
        return {**report, "status": "unchanged", "applied": False, "exact_reviewed_target": True}
    candidate, verification = repair(original, root)
    if path.read_bytes() != original:
        raise ValueError("Source changed during normal reconstruction")
    if apply:
        backup_path = backup / MODEL / VISUAL
        if backup_path.resolve() != backup_path or not backup_path.resolve().is_relative_to(backup):
            raise ValueError("Backup path contains a symlink")
        if backup_path.exists() and (not backup_path.is_file() or backup_path.read_bytes() != original):
            raise ValueError("Backup already contains different content")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if not backup_path.exists():
            with backup_path.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        if path.read_bytes() != original:
            raise ValueError("Source changed after backup preparation")
        atomic_replace(path, candidate)
    else:
        shutil.copytree(source_model, output_model, dirs_exist_ok=True)
        atomic_replace(output_model / VISUAL, candidate)
    return {**report, "status": "applied" if apply else "review_copy", "applied": apply,
            "output_model": None if apply else str(output_model),
            "backup_path": str(backup_path) if apply else None, "verification": verification}


def preflight_report(root, output):
    """Check report creation/replacement before any source asset can change."""
    root = Path(root).resolve()
    destination = report_destination(root, output)
    if destination == (root / "docs/quality/source-normal-outlier-review.json").resolve():
        raise ValueError("Report must not overwrite the historical mask evidence")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        # Opening without O_TRUNC or writes verifies effective permissions while
        # preserving an existing report. NONBLOCK also prevents FIFO hangs.
        descriptor = os.open(destination, os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Existing report must be a regular writable file")
        finally:
            os.close(descriptor)
    # This checks directory writability even when a report already exists.
    # No existing report is truncated or replaced during preflight.
    with tempfile.NamedTemporaryFile(prefix="." + destination.name + ".preflight-", dir=destination.parent):
        pass
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output-model", type=Path)
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        destination = preflight_report(args.root, args.report)
        result = run(args.root, output_model=args.output_model, apply=args.apply, backup_dir=args.backup_dir)
        destination.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({key: result[key] for key in ("status", "applied", "selected_corners", "target_sha256")}))


if __name__ == "__main__":
    main()
