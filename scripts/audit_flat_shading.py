"""Read-only review of authored flat normals on geometrically curved robot surfaces.

Reports are review candidates, never an automatic rewrite list. Existing smooth
groups, small primitives and truly planar constructions are retained. CAD front/
back sheets are diagnosed separately from ordinary manifold edge adjacency.
"""

from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone
import argparse, hashlib, itertools, json, sys
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from glb_format import read_glb
from prepare_preview import accessor_array
from analyze_render_geometry import visible_meshes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Optional inventory filter; always inspect current source bytes",
    )
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    baseline = args.baseline.resolve() if args.baseline else None
    records = []
    paths = sorted((root / "RobotModel").glob("*/*/Visual/glb/*.glb"))
    if baseline is not None:
        paths = [p for p in paths if (baseline / p.relative_to(root)).is_file()]
    for p in paths:
        raw = p.read_bytes()
        tree, binary = read_glb(raw)
        rec = {
            "path": p.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "primitives": [],
        }
        for mi in visible_meshes(tree):
            for pi, pr in enumerate(tree["meshes"][mi]["primitives"]):
                if pr.get("mode", 4) != 4:
                    continue
                attrs = pr["attributes"]
                v = accessor_array(tree, binary, attrs["POSITION"])
                f = accessor_array(tree, binary, pr["indices"]).reshape(-1, 3)
                r = {
                    "mesh": mi,
                    "primitive": pi,
                    "material": pr.get("material"),
                    "triangles": len(f),
                }
                if "NORMAL" not in attrs:
                    r["classification"] = "missing_authored_normals"
                    rec["primitives"].append(r)
                    continue
                n = accessor_array(tree, binary, attrs["NORMAL"])[f]
                flat = np.linalg.norm(n - n[:, :1], axis=2).max(axis=1) <= 1e-6
                r["flat_authored_triangles"] = int(flat.sum())
                if not flat.all():
                    r["classification"] = "preserve_authored_smooth_group"
                    rec["primitives"].append(r)
                    continue
                if len(f) < 100:
                    r["classification"] = "small_group"
                    rec["primitives"].append(r)
                    continue
                pos, weld = np.unique(v, axis=0, return_inverse=True)
                wf = weld[f]
                tris = v[f].astype(float)
                cross = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
                length = np.linalg.norm(cross, axis=1)
                fn = cross / np.maximum(length[:, None], 1e-30)
                face_dot = np.einsum("fc,fvc->fv", fn, n)
                r["mean_authored_normal_face_alignment"] = float(face_dot.mean())
                normalized = n.astype(float) / np.maximum(
                    np.linalg.norm(n.astype(float), axis=2)[:, :, None], 1e-30
                )
                r["max_authored_to_face_direction_error"] = float(
                    np.linalg.norm(normalized - fn[:, None], axis=2).max()
                )
                r["normal_refinement_direction_gate_passed"] = (
                    r["max_authored_to_face_direction_error"] <= 2e-3
                )
                edges = np.concatenate([wf[:, [0, 1]], wf[:, [1, 2]], wf[:, [2, 0]]])
                faceids = np.tile(np.arange(len(f)), 3)
                canonical = np.sort(edges, axis=1)
                order = np.lexsort((canonical[:, 1], canonical[:, 0]))
                ce = canonical[order]
                starts = np.r_[
                    0, np.flatnonzero(np.any(np.diff(ce, axis=0), axis=1)) + 1
                ]
                sizes = np.diff(np.r_[starts, len(order)])
                angles = []
                pair_counter = Counter()
                high_valence = int((sizes > 8).sum())
                for k in range(2, 9):
                    gs = starts[sizes == k]
                    if not len(gs):
                        continue
                    combinations = np.array(list(itertools.combinations(range(k), 2)))
                    a = order[(gs[:, None] + combinations[None, :, 0]).ravel()]
                    b = order[(gs[:, None] + combinations[None, :, 1]).ravel()]
                    opposite = np.all(edges[a] == edges[b][:, ::-1], axis=1) & (
                        faceids[a] != faceids[b]
                    )
                    a, b = a[opposite], b[opposite]
                    dot = np.einsum("ij,ij->i", fn[faceids[a]], fn[faceids[b]])
                    deg = np.degrees(np.arccos(np.clip(dot, -1, 1)))
                    angles.extend(deg.tolist())
                    pair_counter[
                        "manifold_oriented_pairs"
                        if k == 2
                        else "nonmanifold_oriented_pairs"
                    ] += len(deg)
                angles = np.array(angles)
                bins = [0, 0.5, 5, 15, 30, 60, 90, 120, 180.0001]
                hist = np.histogram(angles, bins=bins)[0]
                soft = (angles > 0.5) & (angles <= 30 + 1e-7)
                r.update(
                    geometric_dihedral_histogram_degrees={
                        f"{a:g}-{b:g}": int(h)
                        for a, b, h in zip(bins[:-1], bins[1:], hist)
                    },
                    oriented_edge_pairs=len(angles),
                    curved_pairs_within_30_degrees=int(soft.sum()),
                    edge_pair_counts=dict(pair_counter),
                    nonmanifold_edge_groups=int((sizes > 2).sum()),
                    edge_groups_over_valence_8=high_valence,
                    smooth_curvature_fraction=float(soft.mean()) if len(soft) else 0,
                )
                if (
                    int(soft.sum()) >= 30
                    and r["smooth_curvature_fraction"] >= 0.03
                    and r["mean_authored_normal_face_alignment"] > 0.9
                ):
                    r["classification"] = "flat_curved_shading_candidate"
                    r["needs_oriented_sheet_smoothing"] = bool((sizes > 2).any())
                else:
                    r["classification"] = "flat_geometry_or_insufficient_curvature"
                rec["primitives"].append(r)
        records.append(rec)
    models = defaultdict(
        lambda: {
            "files": [],
            "candidate_triangles": 0,
            "candidate_primitives": 0,
            "soft_edge_pairs": 0,
            "oriented_sheet_handling_files": [],
        }
    )
    for rec in records:
        cand = [
            p
            for p in rec["primitives"]
            if p["classification"] == "flat_curved_shading_candidate"
        ]
        if not cand:
            continue
        model = "/".join(rec["path"].split("/")[:3])
        m = models[model]
        m["files"].append(rec["path"])
        m["candidate_triangles"] += sum(p["triangles"] for p in cand)
        m["candidate_primitives"] += len(cand)
        m["soft_edge_pairs"] += sum(p["curved_pairs_within_30_degrees"] for p in cand)
        if any(p["needs_oriented_sheet_smoothing"] for p in cand):
            m["oriented_sheet_handling_files"].append(rec["path"])
    ranked = [
        {"model": model, **values}
        for model, values in sorted(
            models.items(), key=lambda item: item[1]["soft_edge_pairs"], reverse=True
        )
    ]
    summary = {
        "visual_glbs": len(records),
        "baseline_inventory_filter": baseline is not None,
        "review_candidate_models": len(ranked),
        "candidate_visual_glbs": sum(len(m["files"]) for m in ranked),
        "candidate_primitives": sum(m["candidate_primitives"] for m in ranked),
        "candidate_triangles": sum(m["candidate_triangles"] for m in ranked),
        "all_primitive_classifications": dict(
            Counter(p["classification"] for r in records for p in r["primitives"])
        ),
    }
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": f"Current bytes at {len(records)} robot visual GLB paths"
        + (" present in the supplied baseline inventory" if baseline else "")
        + "; no assets rewritten.",
        "source_script": "scripts/audit_flat_shading.py",
        "interpretation": "Candidates only: fully flat authored normals plus >=30 oriented adjacent edge pairs with geometric dihedral in (0.5,30] degrees. Existing authored smooth groups, small groups and flat-only constructions are retained. Nonmanifold paired CAD front/back surfaces require oriented-sheet fan handling; ordinary manifold smoothing can leave them flat.",
        "summary": summary,
        "ranked_models": ranked,
        "files": records,
    }
    p = args.json
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(summary))
    for m in ranked[:20]:
        print(
            m["model"],
            "faces",
            m["candidate_triangles"],
            "softpairs",
            m["soft_edge_pairs"],
            "files",
            len(m["files"]),
            "sheet",
            len(m["oriented_sheet_handling_files"]),
        )


if __name__ == "__main__":
    main()
