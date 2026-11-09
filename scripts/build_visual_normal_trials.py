"""Build independent, scoped normal review assets; never apply them to the repo.

The scope file must name frozen source GLBs, source hashes and exact primitive
selections. Every proposed file passes the independent stdlib verifier before it
is written, followed by the official Khronos validator over the complete trial.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from glb_format import read_glb, validate_glb
from refine_materials import encode_glb
from refine_visual_normals import refine
from smooth_oriented_sheets import smooth_oriented_normals
from verify_normal_updates import verify_normal_update

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    pending.replace(path)


def fingerprints():
    names = (
        "build_visual_normal_trials.py",
        "refine_visual_normals.py",
        "smooth_oriented_sheets.py",
        "verify_normal_updates.py",
        "glb_format.py",
    )
    return {name: sha((ROOT / "scripts" / name).read_bytes()) for name in names}


def summarise(scope, records, official_errors=0):
    counts = Counter(record["status"] for record in records)
    changed = [
        record
        for record in records
        if record["status"] == "trial" and record["changed"]
    ]
    return {
        **scope["summary"],
        "processed_files": len(records),
        "changed_files": len(changed),
        "changed_models": len({record["model"] for record in changed}),
        "unchanged_files": counts["unchanged"],
        "rejected_files": counts["rejected"],
        "skipped_files": counts["skipped"],
        "changed_normal_corners": sum(
            record.get("verification", {}).get("normal_corners_changed_bytes", 0)
            for record in changed
        ),
        "protected_flat_faces": sum(
            edit.get("protected_faces", 0)
            for record in changed
            for edit in record.get("primitives", [])
            if edit["status"] == "refined"
        ),
        "source_changed_bytes": sum(record["source_bytes"] for record in changed),
        "trial_changed_bytes": sum(record["output_bytes"] for record in changed),
        "newly_opposed_normal_corners": sum(
            record.get("verification", {}).get("newly_opposed_normal_corners", 0)
            for record in records
        ),
        "khronos_errors": official_errors,
        "errors": counts["rejected"] + counts["skipped"] + official_errors,
    }


def process(specification, source, output):
    relative = Path(specification["path"])
    path, destination = (source / relative).resolve(), (output / relative).resolve()
    if (
        not path.is_relative_to(source)
        or not destination.is_relative_to(output)
        or path.suffix != ".glb"
    ):
        raise ValueError("Scope path escapes its source/trial root")
    record = {
        key: specification[key]
        for key in (
            "path",
            "model",
            "source_sha256",
            "eligible_primitives",
            "urdf_refs",
        )
    }
    record.update(changed=False, trial_only=True)
    try:
        original = path.read_bytes()
        if sha(original) != specification["source_sha256"]:
            raise ValueError("Frozen source no longer matches its scoped SHA-256")
        record["source_bytes"] = len(original)
        tree, binary = read_glb(original)
        selection = {tuple(key) for key in specification["eligible_primitives"]}
        target, target_bin, edits = refine(
            tree,
            binary,
            angle=30,
            smoother=smooth_oriented_normals,
            preserve_direction_outliers=True,
            eligible_primitives=selection,
        )
        candidate = encode_glb(target, target_bin)
        validate_glb(candidate)
        verification = verify_normal_update(
            original, candidate, eligible_primitives=selection
        )
        record.update(
            verification=verification,
            primitives=edits,
            geometry_corner_bytes_exact=True,
            non_selected_attributes_byte_exact=True,
            protected_normal_bytes_exact=True,
            materials_nodes_scenes_unchanged=True,
        )
        if verification["newly_opposed_normal_corners"]:
            raise ValueError("Proposed normals oppose previously valid source winding")
        if verification["normal_corners_changed_bytes"] == 0:
            record.update(
                status="unchanged",
                output_sha256=specification["source_sha256"],
                output_bytes=len(original),
                reason="Selected groups have no admissible normal changes; original file retained",
            )
            destination.unlink(missing_ok=True)
            return record
        if sha(path.read_bytes()) != specification["source_sha256"]:
            raise ValueError("Frozen source changed during trial generation")
        destination.parent.mkdir(parents=True, exist_ok=True)
        pending = destination.with_name(destination.name + ".pending")
        pending.write_bytes(candidate)
        pending.replace(destination)
        record.update(
            status="trial",
            changed=True,
            output_sha256=sha(candidate),
            output_bytes=len(candidate),
        )
    except ValueError as error:
        destination.unlink(missing_ok=True)
        record.update(
            status="skipped"
            if str(error).startswith(
                (
                    "Unsupported",
                    "Buffer ",
                    "Accessor ",
                    "Morph ",
                    "Morphing",
                    "Non-indexed",
                )
            )
            else "rejected",
            reason=str(error),
        )
    except Exception as error:
        destination.unlink(missing_ok=True)
        record.update(status="rejected", reason=f"{type(error).__name__}: {error}")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--gltf-report", type=Path, required=True)
    parser.add_argument(
        "--validator-node-path",
        type=Path,
        help="Directory containing gltf-validator for NODE_PATH",
    )
    args = parser.parse_args()
    scope_bytes = args.scope.read_bytes()
    scope = json.loads(scope_bytes)
    source, output = Path(scope["source_root"]).resolve(), args.output.resolve()
    if (
        output.is_relative_to(source)
        or source.is_relative_to(output)
        or output.is_relative_to(ROOT)
    ):
        parser.error(
            "Trial output must be external to the source snapshot and repository"
        )
    output.mkdir(parents=True, exist_ok=True)
    marker_path = output / "trial-marker.json"
    marker = {
        "purpose": "AssetsModel scoped visual normal review trials",
        "trial_only": True,
        "source_root": str(source),
        "scope_sha256": sha(scope_bytes),
    }
    if marker_path.exists():
        if json.loads(marker_path.read_text()) != marker:
            parser.error("Existing trial directory belongs to a different scope/source")
    elif any(output.iterdir()):
        parser.error("An existing nonempty output requires a matching trial marker")
    else:
        write_json(marker_path, marker)
    initial_fingerprints = fingerprints()
    previous = {}
    if args.report.exists():
        candidate = json.loads(args.report.read_text())
        if (
            candidate.get("scope_sha256") == sha(scope_bytes)
            and candidate.get("script_sha256") == initial_fingerprints
            and candidate.get("source_root") == str(source)
        ):
            previous = {record["path"]: record for record in candidate.get("files", [])}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "in_progress",
        "pid": os.getpid(),
        "trial_only": True,
        "source_root": str(source),
        "trial_root": str(output),
        "scope_report": str(args.scope),
        "scope_sha256": sha(scope_bytes),
        "normal_method": "oriented_sheets",
        "preserve_direction_outliers": True,
        "primitive_scope": "audit-selected primitive keys only; all other authored corner attributes retained",
        "script_sha256": initial_fingerprints,
        "files": [],
        "summary": summarise(scope, []),
    }
    write_json(args.report, report)
    for index, specification in enumerate(scope["files"]):
        cached = previous.get(specification["path"])
        if cached and cached["status"] in ("trial", "unchanged"):
            path, target = (
                source / specification["path"],
                output / specification["path"],
            )
            source_matches = sha(path.read_bytes()) == cached["source_sha256"]
            target_matches = (
                cached["status"] == "unchanged"
                or target.is_file()
                and sha(target.read_bytes()) == cached["output_sha256"]
            )
            if source_matches and target_matches:
                record = cached
            else:
                record = process(specification, source, output)
        else:
            record = process(specification, source, output)
        report["files"].append(record)
        report["summary"] = summarise(scope, report["files"])
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_json(args.report, report)
        print(
            f"{index + 1}/{len(scope['files'])} {record['status']} {record['path']} {record.get('reason', '')}",
            flush=True,
        )
    report["status"] = "validating_gltf"
    write_json(args.report, report)
    official_errors = 0
    if any(record["status"] == "trial" for record in report["files"]):
        env = os.environ.copy()
        if args.validator_node_path:
            env["NODE_PATH"] = str(args.validator_node_path.resolve())
        result = subprocess.run(
            [
                "node",
                str(ROOT / "scripts/validate_glb.cjs"),
                "--root",
                str(output),
                "--json",
                str(args.gltf_report),
            ],
            env=env,
        )
        if not args.gltf_report.is_file():
            official_errors = 1
            report["validation_failure"] = "Official validator did not write its report"
        else:
            official = json.loads(args.gltf_report.read_text())
            official_errors = official["summary"]["errors"]
            report["khronos"] = {"report": str(args.gltf_report), **official["summary"]}
            by_path = {item["path"]: item for item in official["meshes"]}
            for record in report["files"]:
                if record["status"] == "trial":
                    item = by_path[record["path"]]
                    if item["sha256"] != record["output_sha256"]:
                        raise ValueError(
                            "Official validator saw a different trial payload"
                        )
                    record["khronos_errors"] = item["issues"]["numErrors"]
                    record["khronos_warnings"] = item["issues"]["numWarnings"]
        if result.returncode and not official_errors:
            official_errors = 1
    if fingerprints() != initial_fingerprints:
        report["code_changed_during_execution"] = True
        official_errors += 1
    report["summary"] = summarise(scope, report["files"], official_errors)
    report["changed_models"] = sorted(
        {
            record["model"]
            for record in report["files"]
            if record["status"] == "trial" and record["changed"]
        }
    )
    report["status"] = "complete"
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(args.report, report)
    print(json.dumps(report["summary"]), flush=True)
    return int(report["summary"]["errors"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
