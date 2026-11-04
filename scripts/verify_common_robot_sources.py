"""Independent read-only provenance, kinematics, geometry and portability audit."""

import argparse, copy, hashlib, json, math, shutil, subprocess, sys, tarfile, tempfile, xml.etree.ElementTree as E
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import collada, trimesh, xacro, yourdfpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from glb_format import read_glb
from prepare_preview import joint_frames
from audit_visual_quality import audit as audit_visual
from validate_assets import validate_urdf

BASE = Path("/tmp/assetsmodel-newrobots-20261103")
MODELS = {
    "xArm6": (
        "UFACTORY",
        "xarm",
        "xarm_description",
        "urdf/xarm_device.urdf.xacro",
        {
            "dof": "6",
            "robot_type": "xarm",
            "add_gripper": "false",
            "mesh_suffix": "dae",
        },
    ),
    "Lite6": (
        "UFACTORY",
        "xarm",
        "xarm_description",
        "urdf/xarm_device.urdf.xacro",
        {
            "dof": "6",
            "robot_type": "lite",
            "add_gripper": "false",
            "mesh_suffix": "dae",
        },
    ),
    "iiwa14": ("KUKA", "iiwa", "iiwa_description", "urdf/iiwa14.urdf.xacro", {}),
    "Gen3": (
        "Kinova",
        "kortex",
        "kortex_description",
        "arms/gen3/7dof/urdf/GEN3-7DOF-NOVISION_FOR_URDF_ARM_V12.urdf",
        {},
    ),
}


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def sig(e):
    if e is None:
        return None
    return [e.tag, dict(e.attrib), [sig(c) for c in e if isinstance(c.tag, str)]]


def canon(e):
    e = copy.deepcopy(e)
    for m in e.findall(".//mesh"):
        m.set("filename", "RESOURCE")
    return sig(e)


def expanded(source, package, input_, args, work):
    if not input_.endswith(".xacro"):
        return E.parse(source / package / input_).getroot()
    pkg = work / package
    shutil.copytree(source / package / "urdf", pkg / "urdf")
    if (source / package / "config").exists():
        shutil.copytree(source / package / "config", pkg / "config")
    for p in pkg.rglob("*.xacro"):
        p.write_text(p.read_text().replace("$(find " + package + ")", str(pkg)))
    return E.fromstring(
        xacro.process_file(str(pkg / input_), mappings=copy.deepcopy(args)).toxml()
    )


def source_triangles(path):
    if path.suffix.lower() == ".dae":
        doc = collada.Collada(str(path))
        result = []
        for geom in doc.scene.objects("geometry"):
            for p in geom.primitives():
                if isinstance(p, collada.polylist.BoundPolylist):
                    p = p.triangleset()
                if isinstance(p, collada.triangleset.BoundTriangleSet):
                    result.append(
                        (
                            p.vertex[p.vertex_index] * (doc.assetInfo.unitmeter or 1)
                        ).astype("f4")
                    )
        return np.concatenate(result)
    scene = trimesh.load_scene(path, process=False)
    return np.concatenate(
        [
            trimesh.transform_points(scene.geometry[name].vertices, mat)[
                scene.geometry[name].faces
            ]
            for node in scene.graph.nodes_geometry
            for mat, name in [scene.graph[node]]
            if isinstance(scene.geometry[name], trimesh.Trimesh)
        ]
    ).astype("f4")


def glb_triangles(path):
    scene = trimesh.load_scene(path, process=False)
    return np.concatenate(
        [
            trimesh.transform_points(scene.geometry[name].vertices, mat)[
                scene.geometry[name].faces
            ]
            for node in scene.graph.nodes_geometry
            for mat, name in [scene.graph[node]]
            if isinstance(scene.geometry[name], trimesh.Trimesh)
        ]
    ).astype("f4")


def canonical(tris):
    # Rotate corners cyclically to the lexicographically smallest start; preserve winding.
    points = np.ascontiguousarray(tris, dtype="<f4").copy()
    points[points == 0] = 0
    order = np.lexsort((points[:, :, 2], points[:, :, 1], points[:, :, 0]), axis=1)[
        :, 0
    ]
    idx = (order[:, None] + np.arange(3)) % 3
    data = np.take_along_axis(points, idx[:, :, None], axis=1).reshape(-1, 9)
    return data[np.lexsort(tuple(data[:, i] for i in reversed(range(9))))]


def audit_model(name):
    vendor, sourcekey, package, input_, args = MODELS[name]
    folder = ROOT / "RobotModel" / vendor / name
    target = folder / (name + ".urdf")
    provenance = json.loads((folder / "provenance.json").read_text())
    source = BASE / sourcekey
    result = {
        "model": name,
        "urdf": str(target.relative_to(ROOT)),
        "upstream": provenance["upstream"],
        "issues": [],
    }
    check = validate_urdf(target, glb_only=True)
    if check["errors"]:
        raise ValueError(check["errors"])
    result["asset_validation"] = check
    manifest_path = source / ".assetsmodel-source.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        assert manifest["commit"] == provenance["upstream"]["commit"], (
            "Source tree manifest commit mismatch"
        )
        verified = []
        for rel, expected in manifest["files"].items():
            file = source / rel
            if not file.is_file() or not (
                rel == "LICENSE" or rel.startswith(package + "/")
            ):
                continue
            data = file.read_bytes()
            blob = hashlib.sha1(
                b"blob " + str(len(data)).encode() + b"\0" + data
            ).hexdigest()
            assert blob == expected, "Fixed-commit Git blob mismatch " + rel
            verified.append(rel)
        result["source_git_blob_verified_files"] = len(verified)
        result["source_tree_manifest_sha256"] = sha(manifest_path)
        assert package + "/" + input_ in verified and "LICENSE" in verified
    else:
        commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        assert commit == provenance["upstream"]["commit"], (
            "Source checkout commit mismatch"
        )
        rows = subprocess.check_output(
            ["git", "-C", str(source), "ls-tree", "-r", "HEAD"], text=True
        ).splitlines()
        verified = []
        for row in rows:
            metadata, rel = row.split("\t")
            parts = metadata.split()
            if parts[1] != "blob" or not (
                rel == "LICENSE" or rel.startswith(package + "/")
            ):
                continue
            file = source / rel
            if not file.is_file():
                continue
            data = file.read_bytes()
            blob = hashlib.sha1(
                b"blob " + str(len(data)).encode() + b"\0" + data
            ).hexdigest()
            assert blob == parts[2], "Source checkout differs from pinned tree: " + rel
            verified.append(rel)
        assert package + "/" + input_ in verified and "LICENSE" in verified
        result["source_checkout_commit"] = commit
        result["source_git_blob_verified_files"] = len(verified)

    result["license_sha256"] = sha(folder / "LICENSE")
    result["license_matches_upstream"] = sha(folder / "LICENSE") == sha(
        source / "LICENSE"
    )
    assert result["license_matches_upstream"]
    notice = (folder / "NOTICE").read_text()
    assert (
        provenance["upstream"]["commit"] in notice
        and provenance["upstream"]["url"] in notice
    )
    if sourcekey == "iiwa" and (BASE / "iiwa.tar.gz").is_file():
        with tarfile.open(BASE / "iiwa.tar.gz") as archive:
            result["source_archive_commit"] = archive.pax_headers.get("comment")
        assert result["source_archive_commit"] == provenance["upstream"]["commit"]
    with tempfile.TemporaryDirectory(prefix="assetsmodel-independent-audit-") as tmp:
        work = Path(tmp)
        original = expanded(source, package, input_, args, work / "expanded")
        imported = E.parse(target).getroot()
        sourcej = {j.get("name"): j for j in original.findall("joint")}
        targetj = {j.get("name"): j for j in imported.findall("joint")}
        assert sourcej.keys() == targetj.keys(), "Joint set changed"
        unused_axes = {
            x["joint"]: x for x in provenance.get("omitted_unused_fixed_axes", [])
        }
        for key in sourcej:
            srcjoint = copy.deepcopy(sourcej[key])
            if key in unused_axes:
                axis = srcjoint.find("axis")
                assert (
                    srcjoint.get("type") == "fixed"
                    and axis is not None
                    and np.array_equal(
                        [float(x) for x in axis.get("xyz").split()], [0, 0, 0]
                    )
                ), "Invalid fixed-axis omission " + key
                assert targetj[key].find("axis") is None
                srcjoint.remove(axis)
            assert sig(srcjoint) == sig(targetj[key]), "Joint changed: " + key
        result["omitted_unused_fixed_axes"] = provenance.get(
            "omitted_unused_fixed_axes", []
        )
        result["joint_subtrees_exact"] = len(sourcej)
        sourcel = {l.get("name"): l for l in original.findall("link")}
        targetl = {l.get("name"): l for l in imported.findall("link")}
        assert sourcel.keys() == targetl.keys(), "Link set changed"
        omitted = {x["link"]: x for x in provenance.get("omitted_invalid_inertias", [])}
        inertias = []
        for key in sourcel:
            s, t = sourcel[key], targetl[key]
            for kind in ("visual", "collision"):
                sv, tv = s.findall(kind), t.findall(kind)
                assert len(sv) == len(tv), "Geometry count changed " + key + " " + kind
                for a, b in zip(sv, tv):
                    assert canon(a) == canon(b), (
                        "Visual/collision metadata changed " + key + " " + kind
                    )
            a, b = s.find("inertial"), t.find("inertial")
            if key in omitted:
                assert a is not None and b is None, "Omission mismatch " + key
                assert sig(a) == sig(E.fromstring(omitted[key]["source_xml"]))
                d = {k: float(v) for k, v in a.find("inertia").attrib.items()}
                matrix = [
                    [d["ixx"], d["ixy"], d["ixz"]],
                    [d["ixy"], d["iyy"], d["iyz"]],
                    [d["ixz"], d["iyz"], d["izz"]],
                ]
                assert np.linalg.eigvalsh(matrix).min() <= 0, (
                    "Omitted inertia was valid " + key
                )
                inertias.append(
                    {
                        "link": key,
                        "status": "source_invalid_omitted",
                        "source_xml": omitted[key]["source_xml"],
                    }
                )
            else:
                assert sig(a) == sig(b), "Inertia changed " + key
                if a is not None:
                    inertias.append(
                        {
                            "link": key,
                            "status": "exact",
                            "mass_kg": float(a.find("mass").get("value")),
                        }
                    )
        result["link_geometry_metadata_exact"] = len(sourcel)
        result["inertias"] = inertias
        result["global_urdf_materials_exact"] = [
            sig(m) for m in original.findall("material")
        ] == [sig(m) for m in imported.findall("material")]
        assert result["global_urdf_materials_exact"]
        meshresults = []
        for rec in provenance["meshes"]:
            inputpath = source / package / rec["source"]
            outputpath = folder / rec["output"]
            assert sha(inputpath) == rec["source_sha256"], "Source SHA mismatch " + str(
                inputpath
            )
            assert sha(outputpath) == rec["output_sha256"], (
                "Output provenance SHA mismatch " + str(outputpath)
            )
            src = canonical(source_triangles(inputpath))
            out = canonical(glb_triangles(outputpath))
            assert src.shape == out.shape, "Triangle count mismatch " + rec["output"]
            exact = np.array_equal(src, out)
            delta = float(np.max(np.abs(src.astype(float) - out.astype(float))))
            assert exact or delta < 1e-7, (
                "Triangle corners/winding differ " + rec["output"] + " " + str(delta)
            )
            item = {
                "source": rec["source"],
                "output": rec["output"],
                "source_sha256": sha(inputpath),
                "output_sha256": sha(outputpath),
                "provenance_output_sha_matches": sha(outputpath)
                == rec["output_sha256"],
                "triangles": len(src),
                "source_corners_exact_float32": exact,
                "max_corner_difference_m": delta,
            }
            if rec["output"].startswith("Visual/"):
                item["visual_quality"] = audit_visual(outputpath)
            meshresults.append(item)
        result["meshes"] = meshresults
        # The target model alone must be portable, including licensing and all resources.
        copyfolder = work / "copied-model"
        shutil.copytree(folder, copyfolder)
        copied = copyfolder / target.name
        assert not validate_urdf(copied, glb_only=True)["errors"]
        for m in E.parse(copied).findall(".//mesh"):
            resolved = (copyfolder / m.get("filename")).resolve()
            assert resolved.is_relative_to(copyfolder) and resolved.is_file()
        robot = yourdfpy.URDF.load(
            str(copied), build_collision_scene_graph=True, load_collision_meshes=True
        )
        # Independently load the source FK without depending on source resource URI handlers.
        reference = copy.deepcopy(original)
        for link in reference.findall("link"):
            for kind in ("visual", "collision"):
                for el in link.findall(kind):
                    link.remove(el)
        refpath = work / "source-fk.urdf"
        E.ElementTree(reference).write(refpath)
        ref = yourdfpy.URDF.load(
            str(refpath), load_meshes=False, load_collision_meshes=False
        )
        poses = []
        for fraction in (0.25, 0.50, 0.75):
            values = {}
            for joint in robot.actuated_joints:
                if joint.type == "continuous":
                    values[joint.name] = (fraction - 0.5) * 2
                else:
                    values[joint.name] = (
                        1 - fraction
                    ) * joint.limit.lower + fraction * joint.limit.upper
            frames, resolved = joint_frames(imported, values)
            robot.update_cfg(values)
            ref.update_cfg(values)
            maxerr = max(
                float(np.max(np.abs(frames[k] - robot.scene.graph.get(k)[0])))
                for k in frames
            )
            maxsource = max(
                float(
                    np.max(
                        np.abs(ref.scene.graph.get(k)[0] - robot.scene.graph.get(k)[0])
                    )
                )
                for k in frames
            )
            assert maxerr < 1e-12 and maxsource < 1e-12
            bounds = np.asarray(robot.scene.bounds)
            collisionbounds = np.asarray(robot.collision_scene.bounds)
            assert np.isfinite(bounds).all() and np.isfinite(collisionbounds).all()
            assert (
                max(bounds[1] - bounds[0]) < 4 and max(bounds[1] - bounds[0]) > 0.1
            ), "Implausible physical size"
            leafs = sorted(
                set(frames)
                - {j.find("parent").get("link") for j in imported.findall("joint")}
            )
            poses.append(
                {
                    "fraction_of_joint_range": fraction,
                    "joints": values,
                    "max_preview_fk_matrix_error": maxerr,
                    "max_source_fk_matrix_error": maxsource,
                    "visual_bounds_m": bounds.tolist(),
                    "collision_bounds_m": collisionbounds.tolist(),
                    "leaf_positions_m": {k: frames[k][:3, 3].tolist() for k in leafs},
                }
            )
        result["poses"] = poses
        result["standalone_copy_load"] = "passed"
    result["status"] = "passed"
    if name == "iiwa14":
        result["source_parameter_note"] = (
            "Upstream URDF uses generic max_effort=300/max_velocity=10 and source inertias; fidelity checked, manufacturer calibration/accuracy not asserted."
        )
    return result


def main():
    global ROOT, BASE
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    p.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="Parent of pinned xarm/, iiwa/, kortex/ source trees",
    )
    p.add_argument("--assets", type=Path, default=ROOT)
    p.add_argument("--json", "--output", dest="output", type=Path, required=True)
    a = p.parse_args()
    ROOT = a.assets.resolve()
    BASE = a.source_root.resolve()
    models = []
    for name in a.models:
        try:
            r = audit_model(name)
        except Exception as e:
            r = {"model": name, "status": "error", "error": str(e)}
        models.append(r)
        print(name, r["status"], r.get("error", ""), flush=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Read-only independent expansion/metadata/triangle/FK/standalone-copy audit; all limits and valid source inertias must match, invalid omissions must be documented. Source corner comparison preserves triangle winding and permits only float32 rounding.",
        "upstream_source_root": str(BASE),
        "source_script": "scripts/verify_common_robot_sources.py",
        "models": models,
        "summary": {
            "models": len(models),
            "errors": sum(m["status"] != "passed" for m in models),
            "legal_poses": sum(len(m.get("poses", [])) for m in models),
            "glbs": sum(len(m.get("meshes", [])) for m in models),
        },
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2) + "\n")
    return int(report["summary"]["errors"] > 0)


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit(
            "This verification script requires Python without -O so all evidence checks execute."
        )
    raise SystemExit(main())
