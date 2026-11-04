"""Render a prepared model with its PBR materials in a repeatable studio.

blender -b --factory-startup --threads 4 --python scripts/render_preview.py -- PACK OUT.png
"""

import argparse
import itertools
import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector


def look_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--view", choices=("hero", "front", "right", "rear", "top"), default="hero")
    parser.add_argument("--azimuth", type=float, help="Override view azimuth in degrees, measured from +X")
    parser.add_argument("--elevation", type=float, help="Override view elevation in degrees")
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--samples", type=int, default=48)
    parser.add_argument("--engine", choices=("cycles", "eevee"), default="eevee",
                        help="EEVEE honors glTF single-sided surfaces; Cycles is a double-sided studio approximation")
    parser.add_argument("--transparent", action="store_true")
    parser.add_argument("--theme", choices=("light", "dark"), default="dark")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if min(args.width, args.height, args.samples) < 1:
        parser.error("Resolution and samples must be positive")
    return args


def material_for(part, pack):
    material = bpy.data.materials.new("surface")
    material.use_nodes = True
    shader = material.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = part["color"]
    shader.inputs["Metallic"].default_value = part.get("metallic", 0.12)
    shader.inputs["Roughness"].default_value = part.get("roughness", 0.38)
    # glTF factors are linear, embedded base-color textures are sRGB.
    if part.get("texture"):
        texture = material.node_tree.nodes.new("ShaderNodeTexImage")
        texture.image = bpy.data.images.load(str((pack / part["texture"]).resolve()), check_existing=True)
        multiply = material.node_tree.nodes.new("ShaderNodeMixRGB")
        multiply.blend_type = "MULTIPLY"
        multiply.inputs[0].default_value = 1
        multiply.inputs[2].default_value = part["color"]
        material.node_tree.links.new(texture.outputs["Color"], multiply.inputs[1])
        material.node_tree.links.new(multiply.outputs[0], shader.inputs["Base Color"])
    alpha_mode = part.get("alpha_mode", "OPAQUE")
    if alpha_mode != "OPAQUE":
        alpha = part["color"][3]
        shader.inputs["Alpha"].default_value = (float(alpha >= part.get("alpha_cutoff", .5))
                                                if alpha_mode == "MASK" else alpha)
        if hasattr(material, "surface_render_method"):
            material.surface_render_method = "DITHERED"
    material.use_backface_culling = not part.get("double_sided", True)
    return material


def main():
    args = arguments()
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    parts = json.loads((args.pack / "scene.json").read_text())
    if not parts:
        raise ValueError("Empty preview pack")
    bounds = []
    for part in parts:
        with np.load(args.pack / part["file"]) as data:
            bounds.extend([data["vertices"].min(axis=0), data["vertices"].max(axis=0)])
    bounds = np.asarray(bounds)
    lower, upper = bounds.min(axis=0), bounds.max(axis=0)
    span = float(max(upper - lower))
    if not np.isfinite(bounds).all() or span <= 0:
        raise ValueError("Invalid model bounds")
    scale = 2.0 / span
    center = (lower + upper) / 2
    center[2] = lower[2]
    for index, part in enumerate(parts):
        with np.load(args.pack / part["file"]) as data:
            mesh = bpy.data.meshes.new(f"part-{index}")
            mesh.from_pydata(((data["vertices"] - center) * scale).tolist(), [], data["faces"].tolist())
            mesh.polygons.foreach_set("use_smooth", np.ones(len(mesh.polygons), dtype=bool))
            mesh.normals_split_custom_set(data["normals"].tolist())
            mesh.update()
            if "uv" in data:
                uv = mesh.uv_layers.new(name="UVMap")
                uv.data.foreach_set("uv", data["uv"].reshape(-1))
        obj = bpy.data.objects.new(mesh.name, mesh)
        bpy.context.collection.objects.link(obj)
        mesh.materials.append(material_for(part, args.pack))
    dark = args.theme == "dark"
    bpy.ops.mesh.primitive_plane_add(size=200)
    plane = bpy.context.object
    plane.location.z = -0.012
    floor = bpy.data.materials.new("floor")
    floor.use_nodes = True
    floor_shader = floor.node_tree.nodes.get("Principled BSDF")
    floor_shader.inputs["Base Color"].default_value = (0.045, 0.055, 0.075, 1) if dark else (0.68, 0.71, 0.76, 1)
    floor_shader.inputs["Roughness"].default_value = 0.82
    plane.data.materials.append(floor)
    if args.transparent:
        plane.hide_render = True
    height = (upper[2] - lower[2]) * scale
    target = Vector((0, 0, height * 0.5))
    azimuth, elevation = {"hero": (-55, 23), "front": (-90, 5), "right": (0, 5),
                           "rear": (125, 22), "top": (-90, 89.9)}[args.view]
    azimuth = math.radians(args.azimuth if args.azimuth is not None else azimuth)
    elevation = math.radians(args.elevation if args.elevation is not None else elevation)
    direction = Vector((math.cos(elevation) * math.cos(azimuth),
                        math.cos(elevation) * math.sin(azimuth), math.sin(elevation)))
    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = args.width, args.height
    scene.render.resolution_percentage = 100
    bpy.ops.object.camera_add(location=target + direction * 7)
    camera = bpy.context.object
    camera.data.type = "ORTHO"
    look_at(camera, target)
    # Fit projected bounds, including wide horizontal arms and alternate views.
    rotation = camera.rotation_euler.to_matrix().transposed()
    corners = [rotation @ (Vector(v) - target) for v in itertools.product(*zip((lower - center) * scale, (upper - center) * scale))]
    projected = np.asarray([tuple(v) for v in corners])
    width, projected_height = np.ptp(projected[:, :2], axis=0)
    camera.data.ortho_scale = 1.0
    unit_frame = np.asarray([tuple(v) for v in camera.data.view_frame(scene=scene)])
    frame_width, frame_height = np.ptp(unit_frame[:, :2], axis=0)
    camera.data.ortho_scale = max(projected_height / frame_height, width / frame_width) * 1.20
    scene.camera = camera
    for location, power, size in [((2, -3, 5), 480, 3), ((-3, -1, 3), 120, 3), ((1, 4, 4), 500, 3)]:
        bpy.ops.object.light_add(type="AREA", location=location)
        lamp = bpy.context.object
        lamp.data.energy, lamp.data.shape, lamp.data.size = power, "DISK", size
        look_at(lamp, target)
    scene.world.use_nodes = True
    background = scene.world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.65, 0.70, 0.80, 1)
    background.inputs["Strength"].default_value = 0.16
    scene.render.engine = "CYCLES" if args.engine == "cycles" else "BLENDER_EEVEE"
    if args.engine == "eevee":
        scene.eevee.taa_render_samples = args.samples
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = True
    scene.render.resolution_x, scene.render.resolution_y = args.width, args.height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA" if args.transparent else "RGB"
    scene.render.film_transparent = args.transparent
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.exposure = -0.35
    args.output.parent.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(args.output.resolve())
    bpy.ops.render.render(write_still=True)
    metadata = {"pack": str(args.pack), "view": args.view, "azimuth_degrees": math.degrees(azimuth),
                "elevation_degrees": math.degrees(elevation), "width": args.width, "height": args.height,
                "samples": args.samples, "theme": args.theme, "engine": scene.render.engine, "view_transform": "AgX",
                "single_sided_materials": "native culling" if args.engine == "eevee" else "double-sided approximation",
                "blender": bpy.app.version_string, "parts": len(parts), "bounds_m": [lower.tolist(), upper.tolist()],
                "ortho_scale": camera.data.ortho_scale}
    args.output.with_suffix(".render.json").write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    main()
