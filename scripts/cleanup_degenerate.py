"""Remove zero-area triangles without changing any retained vertex or normal.

Consumes a mesh audit; only files with reported degenerate faces are inspected.
"""

import argparse
import copy
import hashlib
import io
import json
import struct
from pathlib import Path

import collada
import numpy as np
from lxml import etree as ET


def valid_faces(triangles):
    triangles = np.asarray(triangles, dtype=np.float64)
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    return np.linalg.norm(cross, axis=1) > 1e-20


def clean(path):
    data = path.read_bytes()
    removed = 0
    if path.suffix == ".stl":
        count = struct.unpack_from("<I", data, 80)[0]
        if len(data) != 84 + 50 * count:
            raise ValueError("Expected binary STL")
        records = np.frombuffer(data, dtype=np.dtype([
            ("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")
        ]), offset=84)
        keep = valid_faces(records["vertices"])
        removed = int(np.count_nonzero(~keep))
        output = data[:80] + struct.pack("<I", int(keep.sum())) + records[keep].tobytes()
    else:
        doc = collada.Collada(str(path))
        namespace = "{http://www.collada.org/2005/11/COLLADASchema}"
        for geometry in doc.geometries:
            for primitive in geometry.primitives:
                original_node = primitive.xmlnode
                is_polygon = isinstance(primitive, collada.polylist.Polylist)
                if isinstance(primitive, collada.polylist.Polylist):
                    primitive = primitive.triangleset()
                if not isinstance(primitive, collada.triangleset.TriangleSet):
                    continue
                keep = valid_faces(primitive.vertex[primitive.vertex_index])
                if keep.all():
                    continue
                removed += int(np.count_nonzero(~keep))
                node = original_node
                if is_polygon:
                    node = ET.Element(namespace + "triangles")
                    if primitive.material:
                        node.set("material", primitive.material)
                    for element in original_node.findall(namespace + "input"):
                        node.append(copy.deepcopy(element))
                    ET.SubElement(node, namespace + "p")
                    original_node.getparent().replace(original_node, node)
                indices = primitive.index.reshape(len(keep), -1)[keep]
                node.find(namespace + "p").text = " ".join(str(int(x)) for x in indices.flat)
                node.set("count", str(int(keep.sum())))
        output = ET.tostring(doc.xmlnode, encoding="utf-8", xml_declaration=True)
        loaded = collada.Collada(io.BytesIO(output))
        for geometry in loaded.geometries:
            for primitive in geometry.primitives:
                if isinstance(primitive, collada.polylist.Polylist):
                    primitive = primitive.triangleset()
                if isinstance(primitive, collada.triangleset.TriangleSet):
                    primitive.vertex[primitive.vertex_index]
    return output, removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    results = []
    for item in json.loads(args.audit.read_text())["meshes"]:
        if not item.get("degenerate_faces"):
            continue
        path = Path(item["path"])
        original = path.read_bytes()
        output, removed = clean(path)
        if not removed:
            continue
        if args.apply:
            backup = args.backup_dir / path
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open("xb") as stream:
                stream.write(original)
            staging = path.with_suffix(path.suffix + ".tmp")
            staging.write_bytes(output)
            staging.replace(path)
        results.append({"path": path.as_posix(), "removed": removed,
                        "bytes_before": len(original), "bytes_after": len(output),
                        "sha256_before": hashlib.sha256(original).hexdigest(),
                        "sha256_after": hashlib.sha256(output).hexdigest()})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"files": results}, indent=2) + "\n")
    print(f"{len(results)} meshes: removed {sum(x['removed'] for x in results)} zero-area triangles")


if __name__ == "__main__":
    main()
