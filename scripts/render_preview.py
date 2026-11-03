"""Render prepared geometry: blender -b --python scripts/render_preview.py -- PACK OUTPUT.png."""

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector


def look_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def make_material(part, pack, name):
    """Build the packed glTF PBR material; color factors are already linear."""
    properties = part.get("material")
    if properties is None:  # Backward compatibility with historical review packs.
        properties = {"base_color": part["color"], "metallic": 0.12,
                      "roughness": 0.38, "double_sided": True}
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    shader = nodes.get("Principled BSDF")
    color = properties["base_color"]
    material.diffuse_color = color
    shader.inputs["Base Color"].default_value = color
    shader.inputs["Metallic"].default_value = properties.get("metallic", 1.0)
    shader.inputs["Roughness"].default_value = properties.get("roughness", 1.0)
    shader.inputs["Emission Color"].default_value = (*properties.get("emissive", [0, 0, 0]), 1)
    shader.inputs["Emission Strength"].default_value = 1
    alpha_mode = properties.get("alpha_mode", "OPAQUE")
    alpha = nodes.new("ShaderNodeValue")
    alpha.outputs[0].default_value = color[3]
    if "base_color_texture" in properties:
        info = properties["base_color_texture"]
        image = nodes.new("ShaderNodeTexImage")
        image.image = bpy.data.images.load(str((pack / info["file"]).resolve()), check_existing=True)
        image.image.colorspace_settings.name = "sRGB"
        image.extension = info.get("extension", "REPEAT")
        image.interpolation = info.get("interpolation", "Linear")
        multiply = nodes.new("ShaderNodeMixRGB")
        multiply.blend_type = "MULTIPLY"
        multiply.inputs[0].default_value = 1
        multiply.inputs[2].default_value = color
        links.new(image.outputs["Color"], multiply.inputs[1])
        links.new(multiply.outputs[0], shader.inputs["Base Color"])
        factor = nodes.new("ShaderNodeMath")
        factor.operation = "MULTIPLY"
        links.new(image.outputs["Alpha"], factor.inputs[0])
        links.new(alpha.outputs[0], factor.inputs[1])
        alpha = factor
    if alpha_mode == "MASK":
        cutoff = nodes.new("ShaderNodeMath")
        cutoff.operation = "LESS_THAN"
        links.new(alpha.outputs[0], cutoff.inputs[0])
        cutoff.inputs[1].default_value = properties.get("alpha_cutoff", 0.5)
        keep = nodes.new("ShaderNodeMath")
        keep.operation = "SUBTRACT"
        keep.inputs[0].default_value = 1
        links.new(cutoff.outputs[0], keep.inputs[1])
        links.new(keep.outputs[0], shader.inputs["Alpha"])
    elif alpha_mode == "BLEND":
        links.new(alpha.outputs[0], shader.inputs["Alpha"])
    elif alpha_mode != "OPAQUE":
        raise ValueError(f"Unknown alpha mode: {alpha_mode}")
    if not properties.get("double_sided", False):
        # Cycles ignores viewport backface culling. Explicitly make back faces
        # transparent so an open CAD surface is not silently rendered double-sided.
        geometry = nodes.new("ShaderNodeNewGeometry")
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        mix = nodes.new("ShaderNodeMixShader")
        links.new(geometry.outputs["Backfacing"], mix.inputs[0])
        links.new(shader.outputs[0], mix.inputs[1])
        links.new(transparent.outputs[0], mix.inputs[2])
        links.new(mix.outputs[0], nodes.get("Material Output").inputs["Surface"])
        material.use_backface_culling = True
    return material


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--azimuth", type=float, default=0,
                        help="Camera orbit around Z relative to the default view, in degrees")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    pack, output = args.pack, args.output
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
            if "uv" in data:
                layer = mesh.uv_layers.new(name="TEXCOORD_0")
                layer.data.foreach_set("uv", data["uv"].ravel())
            mesh.update()
        obj = bpy.data.objects.new(mesh.name, mesh)
        bpy.context.collection.objects.link(obj)
        mesh.materials.append(make_material(part, pack, mesh.name))
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
    angle = math.radians(args.azimuth)
    x = 3.4 * math.cos(angle) + 4.5 * math.sin(angle)
    y = 3.4 * math.sin(angle) - 4.5 * math.cos(angle)
    bpy.ops.object.camera_add(location=(x, y, 2.8 + height * 0.25))
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


if __name__ == "__main__":
    main()
