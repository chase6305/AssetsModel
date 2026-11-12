"""Blender integration check for opaque CAD sheets with coincident back faces.

Run under the shared heavy-work lock, for example:
blender -b --factory-startup --threads 2 --python-exit-code 1 \
  --python scripts/verify_preview_culling.py -- /tmp/preview-culling.json
"""
import json
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_preview import material_for


def surface(name, front, color, double_sided=False):
    mesh = bpy.data.meshes.new(name)
    indices = [0, 1, 2, 3] if front else [3, 2, 1, 0]
    mesh.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [indices])
    material = material_for({'material': {'base_color': [0, 0, 0, 1], 'metallic': 0,
                            'roughness': .5, 'emissive': color,
                            'double_sided': double_sided}}, Path('.'))
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def main():
    destination = Path(sys.argv[sys.argv.index('--') + 1])
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_EEVEE'
    scene.eevee.taa_render_samples = 8
    scene.render.resolution_x = scene.render.resolution_y = 64
    scene.render.resolution_percentage = 100
    scene.view_settings.view_transform = 'Standard'
    scene.world.color = (0, 0, 0)
    bpy.ops.object.camera_add(location=(0, 0, 3))
    scene.camera = bpy.context.object
    scene.camera.data.type = 'ORTHO'
    scene.camera.data.ortho_scale = 3
    # Cameras face down local -Z by default.
    records = []
    output = destination.parent / (destination.stem + '-renders')
    output.mkdir(parents=True, exist_ok=True)
    scene.render.image_settings.file_format = 'PNG'
    for facing in (True, False):
        for reverse in (False, True):
            for same_object in (False, True):
                specs = [('front', True, [1, .08, 0]), ('back', False, [0, .05, 1])]
                if reverse:
                    specs.reverse()
                objects = [surface(*spec) for spec in specs]
                if same_object:
                    bpy.ops.object.select_all(action='DESELECT')
                    for obj in objects:
                        obj.select_set(True)
                    bpy.context.view_layer.objects.active = objects[0]
                    bpy.ops.object.join()
                    objects = [bpy.context.object]
                scene.camera.location.z = 3 if facing else -3
                scene.camera.rotation_euler = (0, 0, 0) if facing else (0, 3.141592653589793, 0)
                label = f'{"front" if facing else "back"}-order{int(reverse)}-joined{int(same_object)}'
                scene.render.filepath = str(output / (label + '.png'))
                bpy.ops.render.render(write_still=True)
                # Reload PNG: background Blender Render Result may not expose pixels.
                rendered = bpy.data.images.load(scene.render.filepath, check_existing=False)
                offset = (32 * 64 + 32) * 4
                pixel = list(rendered.pixels[offset:offset + 3])
                bpy.data.images.remove(rendered)
                dominant, suppressed = (0, 2) if facing else (2, 0)
                passed = pixel[dominant] > .8 and pixel[suppressed] < .02
                records.append({'case': label, 'center_linear_rgb': pixel, 'passed': passed})
                for obj in objects:
                    bpy.data.objects.remove(obj, do_unlink=True)
    result = {'blender': bpy.app.version_string, 'engine': scene.render.engine,
              'cases': records, 'passed': all(r['passed'] for r in records)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2) + '\n')
    if not result['passed']:
        raise AssertionError('Native culling failed; see ' + str(destination))


if __name__ == '__main__':
    main()
