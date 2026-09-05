"""Small, dependency-free checks for the self-contained GLB 2.0 asset profile."""

import json
import struct
from pathlib import Path

COMPONENTS = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2),
              5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
          "MAT2": 4, "MAT3": 9, "MAT4": 16}


def read_glb(data):
    if isinstance(data, (str, Path)):
        data = Path(data).read_bytes()
    if len(data) < 20:
        raise ValueError("Truncated GLB header")
    magic, version, length = struct.unpack_from("<III", data)
    if magic != 0x46546C67 or version != 2 or length != len(data):
        raise ValueError("Invalid GLB magic, version or length")
    chunks, offset = [], 12
    while offset < length:
        if offset + 8 > length:
            raise ValueError("Truncated GLB chunk header")
        size, kind = struct.unpack_from("<II", data, offset)
        offset += 8
        if size % 4 or offset + size > length:
            raise ValueError("Invalid GLB chunk size/alignment")
        chunks.append((kind, data[offset:offset + size]))
        offset += size
    if not chunks or chunks[0][0] != 0x4E4F534A:
        raise ValueError("GLB must begin with a JSON chunk")
    if len(chunks) != 2 or chunks[1][0] != 0x004E4942:
        raise ValueError("Expected one JSON and one embedded BIN chunk")
    tree = json.loads(chunks[0][1])
    if tree.get("asset", {}).get("version") != "2.0":
        raise ValueError("Expected glTF asset version 2.0")
    return tree, chunks[1][1]


def accessor_bytes(tree, binary, index):
    accessor = tree["accessors"][index]
    if "sparse" in accessor:
        raise ValueError("Sparse accessors are outside the repository GLB profile")
    view = tree["bufferViews"][accessor["bufferView"]]
    if view.get("buffer", 0) != 0:
        raise ValueError("Expected the embedded buffer")
    component, size = COMPONENTS[accessor["componentType"]]
    width = WIDTHS[accessor["type"]]
    packed = size * width
    stride = view.get("byteStride", packed)
    offset = accessor.get("byteOffset", 0)
    count = accessor["count"]
    if count < 1 or stride < packed or stride % size or offset < 0 or offset % size:
        raise ValueError("Invalid accessor count, stride or alignment")
    needed = offset + (count - 1) * stride + packed
    if needed > view["byteLength"]:
        raise ValueError("Accessor exceeds its buffer view")
    start = view.get("byteOffset", 0) + offset
    if stride != packed:
        return b"".join(binary[start + i * stride:start + i * stride + packed] for i in range(count)), component, width
    return binary[start:start + count * packed], component, width


def validate_glb(data):
    tree, binary = read_glb(data)
    for key in ("accessors", "bufferViews", "buffers", "materials", "meshes", "nodes", "scenes"):
        if key in tree and not tree[key]:
            raise ValueError(f"glTF array must not be empty: {key}")
    buffers = tree.get("buffers", [])
    if len(buffers) != 1 or "uri" in buffers[0]:
        raise ValueError("Expected one embedded buffer with no external URI")
    declared = buffers[0]["byteLength"]
    if not 0 <= len(binary) - declared <= 3:
        raise ValueError("BIN length does not match the declared buffer")
    if tree.get("extensionsRequired"):
        raise ValueError("Repository GLB files must not require decoder extensions")
    for view in tree.get("bufferViews", []):
        start = view.get("byteOffset", 0)
        if start < 0 or start % 4 or view["byteLength"] < 0 or start + view["byteLength"] > declared:
            raise ValueError("Invalid buffer view bounds or alignment")
    for index in range(len(tree.get("accessors", []))):
        accessor_bytes(tree, binary, index)
    triangles = lines = 0
    for mesh in tree.get("meshes", []):
        for primitive in mesh["primitives"]:
            position = tree["accessors"][primitive["attributes"]["POSITION"]]
            if position["type"] != "VEC3" or position["componentType"] != 5126:
                raise ValueError("Positions must be float VEC3")
            for index in primitive["attributes"].values():
                if tree["accessors"][index]["count"] != position["count"]:
                    raise ValueError("Vertex attribute counts differ")
            values, component, width = accessor_bytes(tree, binary, primitive["indices"])
            if width != 1 or component not in ("B", "H", "I"):
                raise ValueError("Indices must be unsigned SCALAR")
            if any(x[0] >= position["count"] for x in struct.iter_unpack("<" + component, values)):
                raise ValueError("Index references a missing vertex")
            count = tree["accessors"][primitive["indices"]]["count"]
            mode = primitive.get("mode", 4)
            if mode not in (1, 4) or count % (3 if mode == 4 else 2):
                raise ValueError("Invalid triangle/line index count")
            if mode == 4:
                triangles += count // 3
            else:
                lines += count // 2
    if not triangles:
        raise ValueError("GLB has no triangles")
    return {"faces": triangles, "lines": lines, "materials": len(tree.get("materials", []))}
