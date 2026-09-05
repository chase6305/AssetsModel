"""Render prepared geometry: blender -b --python scripts/render_preview.py -- PACK OUTPUT.png."""

import json
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector


def look_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def main():
    args = sys.argv[sys.argv.index("--") + 1:]
    pack, output = Path(args[0]), Path(args[1])
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    parts = json.loads((pack / "scene.json").read_text())
    bounds = []
    for part in parts:
        with np.load(pack / part["file"]) as data:
            bounds.extend([data["vertices"].min(axis=0), data["vertices"].max(axis=0)])
    bounds = np.array(bounds)
    lower, upper = bounds.min(axis=0), bounds.max(axis=0)
    scale = 2.0 / max(upper - lower)
    center = (lower + upper) / 2
    center[2] = lower[2]
    for index, part in enumerate(parts):
        with np.load(pack / part["file"]) as data:
            mesh = bpy.data.meshes.new(f"part-{index}")
            mesh.from_pydata(((data["vertices"] - center) * scale).tolist(), [], data["faces"].tolist())
            mesh.polygons.foreach_set("use_smooth", np.ones(len(mesh.polygons), dtype=bool))
            mesh.normals_split_custom_set(data["normals"].tolist())
            mesh.update()
        obj = bpy.data.objects.new(mesh.name, mesh)
        bpy.context.collection.objects.link(obj)
        material = bpy.data.materials.new(mesh.name)
        material.use_nodes = True
        shader = material.node_tree.nodes.get("Principled BSDF")
        shader.inputs["Base Color"].default_value = part["color"]
        shader.inputs["Metallic"].default_value = 0.12
        shader.inputs["Roughness"].default_value = 0.38
        mesh.materials.append(material)
    bpy.ops.mesh.primitive_plane_add(size=200)
    plane = bpy.context.object
    plane.location.z = -0.015
    material = bpy.data.materials.new("floor")
    material.use_nodes = True
    shader = material.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (0.055, 0.070, 0.095, 1)
    shader.inputs["Roughness"].default_value = 0.85
    plane.data.materials.append(material)
    height = (upper[2] - lower[2]) * scale
    target = (0, 0, height * 0.5)
    bpy.ops.object.camera_add(location=(3.4, -4.5, 2.8 + height * 0.25))
    camera = bpy.context.object
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 3.15
    look_at(camera, target)
    scene = bpy.context.scene
    scene.camera = camera
    for location, power, size in [((2, -3, 5), 650, 4), ((-3, -1, 3), 450, 3), ((1, 4, 4), 900, 3)]:
        bpy.ops.object.light_add(type="AREA", location=location)
        lamp = bpy.context.object
        lamp.data.energy, lamp.data.shape, lamp.data.size = power, "DISK", size
        look_at(lamp, target)
    scene.world.color = (0.2, 0.2, 0.2)
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 24
    scene.cycles.use_denoising = True
    scene.render.resolution_x = 800
    scene.render.resolution_y = 700
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.exposure = -0.5
    output.parent.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(output.resolve())
    bpy.ops.render.render(write_still=True)


main()
