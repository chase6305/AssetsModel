"""Conservative corner normals for orientable sheets with coincident back faces.

Exact welded positions establish topology only. An edge joins two corner fans
only when its oppositely traversing, crease-compatible half-edges form a unique
mutual pair. This keeps CAD front/back sheets and ambiguous non-manifold edges
separate, without changing any vertex or triangle.
"""
from __future__ import annotations

import math

import numpy as np


def smooth_oriented_normals(vertices, faces, angle=30):
    """Return area-weighted normals shaped ``(len(faces) * 3, 3)``.

    Only triangles sharing an exact geometric edge can connect local corner
    fans. Both half-edges must independently have exactly one compatible
    opposite half-edge, and must choose each other. Face normals must differ
    by at most ``angle`` degrees (0 <= angle < 90), so front/back duplicates
    never mix. Ambiguous adjacency stays disconnected. If an entire fan has
    a cancelling weighted sum, its original geometric face normals are kept.

    Input positions/indices are neither reordered nor modified. This function
    generates shading from geometry; callers must separately decide whether
    replacing an authored normal field is appropriate.
    """
    vertices = np.asarray(vertices)
    faces = np.asarray(faces)
    if vertices.ndim != 2 or vertices.shape[1] != 3 or not np.isfinite(vertices).all():
        raise ValueError('Vertices must be finite XYZ rows')
    if faces.ndim != 2 or faces.shape[1] != 3 or not np.issubdtype(faces.dtype, np.integer):
        raise ValueError('Faces must be integer triangle rows')
    if not math.isfinite(float(angle)) or not 0 <= float(angle) < 90:
        raise ValueError('Smoothing angle must be finite and between 0 and 90 degrees (exclusive upper bound)')
    if not len(faces):
        return np.empty((0, 3), dtype=np.float64)
    if faces.min() < 0 or faces.max() >= len(vertices):
        raise ValueError('Face index is outside the vertex array')
    points = vertices[faces].astype(np.float64)
    vectors = np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0])
    lengths = np.linalg.norm(vectors, axis=1)
    if not np.isfinite(lengths).all() or np.any(lengths <= 0):
        raise ValueError('Degenerate or non-finite triangle cannot supply a face normal')
    face_normals = vectors / lengths[:, None]
    # np.unique equality is exact; signed zero denotes the same position.
    _, welded = np.unique(vertices, axis=0, return_inverse=True)
    welded_faces = welded[faces]
    edge_uses = {}
    for fi, triangle in enumerate(welded_faces):
        for ci in range(3):
            cj = (ci + 1) % 3
            start, end = int(triangle[ci]), int(triangle[cj])
            key = (min(start, end), max(start, end))
            edge_uses.setdefault(key, []).append((fi, ci, cj, start, end))

    parents = np.arange(len(faces) * 3, dtype=np.int64)
    ranks = np.zeros(len(parents), dtype=np.uint8)

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return int(index)

    def join(a, b):
        a, b = find(a), find(b)
        if a == b:
            return
        if ranks[a] < ranks[b]:
            a, b = b, a
        parents[b] = a
        if ranks[a] == ranks[b]:
            ranks[a] += 1

    cosine = math.cos(math.radians(float(angle)))
    for uses in edge_uses.values():
        if len(uses) < 2:
            continue
        choices = [-1] * len(uses)
        for i, (fi, _ci, _cj, start, end) in enumerate(uses):
            for j, (other_fi, _oi, _oj, other_start, other_end) in enumerate(uses):
                if fi == other_fi or start != other_end or end != other_start:
                    continue
                if float(np.dot(face_normals[fi], face_normals[other_fi])) < cosine:
                    continue
                if choices[i] != -1:
                    choices[i] = -2  # More than one compatible neighbour: ambiguous.
                    break
                choices[i] = j
        for i, j in enumerate(choices):
            if j <= i or choices[j] != i:
                continue
            fi, ci, cj, _start, _end = uses[i]
            other_fi, oi, oj, _other_start, _other_end = uses[j]
            join(fi * 3 + ci, other_fi * 3 + oj)
            join(fi * 3 + cj, other_fi * 3 + oi)

    roots = np.fromiter((find(i) for i in range(len(parents))), dtype=np.int64, count=len(parents))
    summed = np.zeros((len(parents), 3), dtype=np.float64)
    weights = np.zeros(len(parents), dtype=np.float64)
    np.add.at(summed, roots, np.repeat(vectors, 3, axis=0))
    np.add.at(weights, roots, np.repeat(lengths, 3))
    fan_vectors = summed[roots]
    fan_lengths = np.linalg.norm(fan_vectors, axis=1)
    valid = fan_lengths > weights[roots] * 1e-12
    result = np.repeat(face_normals, 3, axis=0)
    result[valid] = fan_vectors[valid] / fan_lengths[valid, None]
    return result
