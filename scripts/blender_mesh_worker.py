"""Private NPZ/JSON bridge for the optimizer; run with Blender's Python."""

import json
import math
import sys
import traceback

import bpy
import numpy as np


def process(request):
    data = np.load(request["input"])
    mesh = bpy.data.meshes.new("asset")
    obj = bpy.data.objects.new("asset", mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        mesh.from_pydata(data["vertices"].tolist(), [], data["faces"].tolist())
        mesh.update()
        ratio = request["ratio"]
        if ratio < 1:
            modifier = obj.modifiers.new("Simplify", "DECIMATE")
            modifier.decimate_type = "COLLAPSE"
            modifier.ratio = ratio
            modifier.use_collapse_triangulate = True
            bpy.ops.object.modifier_apply(modifier=modifier.name)
        mesh = obj.data
        # Smooth curved surfaces while retaining mechanical creases.
        mesh.polygons.foreach_set("use_smooth", np.ones(len(mesh.polygons), dtype=bool))
        mesh.set_sharp_from_angle(angle=math.radians(30))
        mesh.update()
        mesh.calc_loop_triangles()
        vertices = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", vertices)
        faces = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", faces)
        loops = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("loops", loops)
        normals = np.empty(len(mesh.corner_normals) * 3, dtype=np.float64)
        mesh.corner_normals.foreach_get("vector", normals)
        np.savez(request["output"], vertices=vertices.reshape(-1, 3),
                 faces=faces.reshape(-1, 3),
                 normals=normals.reshape(-1, 3)[loops].reshape(-1, 3, 3))
    finally:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.meshes.remove(mesh)


for line in sys.stdin:
    try:
        process(json.loads(line))
        result = {"ok": True}
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    print("ASSET_RESULT " + json.dumps(result), flush=True)
