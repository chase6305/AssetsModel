"""Render a model manifest with cached preparation and reproducible source fingerprints.

python scripts/render_catalog.py --manifest docs/quality/preview-models.json \
  --output docs/previews/robots --work /tmp/assetsmodel-render
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(asset, settings, root):
    inputs = {asset}
    if asset.suffix == '.urdf':
        inputs.update((asset.parent / m.get('filename')).resolve() for m in ET.parse(asset).findall('.//mesh'))
    inputs.update(ROOT / 'scripts' / name for name in ('prepare_preview.py', 'render_preview.py', 'render_catalog.py'))
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode())
    for path in sorted(inputs):
        digest.update(str(path.relative_to(root) if path.is_relative_to(root) else path.name).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def contact_sheet(records, output):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return
    cell_w, cell_h, columns = 420, 410, 3
    rows = (len(records) + columns - 1) // columns
    sheet = Image.new('RGB', (cell_w * columns, cell_h * rows), '#edf0f4')
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 15)
    except OSError:
        font = ImageFont.load_default()
    for i, record in enumerate(records):
        x, y = i % columns * cell_w, i // columns * cell_h
        with Image.open(output / record['image']) as source:
            source.thumbnail((cell_w, cell_h - 48))
            sheet.paste(source, (x + (cell_w - source.width) // 2, y))
        draw.text((x + 18, y + cell_h - 39), record['label'], fill='#243446', font=font)
        draw.text((x + 18, y + cell_h - 19), record['view'], fill='#667085', font=font)
    sheet.save(output / 'contact-sheet.jpg', quality=90)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='Alternate asset tree, e.g. pre-change snapshot')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True, help='Prepared mesh packs and render logs')
    parser.add_argument('--blender', default='blender')
    parser.add_argument('--samples', type=int, default=32)
    parser.add_argument('--engine', choices=('cycles', 'eevee'), default='eevee')
    parser.add_argument('--width', type=int, default=900)
    parser.add_argument('--height', type=int, default=800)
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--only', nargs='+', help='Render only these manifest IDs')
    args = parser.parse_args()
    root, output, work = args.root.resolve(), args.output.resolve(), args.work.resolve()
    if not shutil.which(args.blender):
        parser.error('Blender is required')
    entries = json.loads(args.manifest.read_text())['models']
    if args.only:
        entries = [e for e in entries if e['id'] in args.only]
    if not entries:
        parser.error('No models selected')
    ids = [e['id'] for e in entries]
    if len(ids) != len(set(ids)) or any(not x or not all(c.isalnum() or c in '-_' for c in x) for x in ids):
        parser.error('Model IDs must be unique portable names')
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    records = []
    for entry in entries:
        asset = (root / entry['urdf']).resolve()
        if not asset.is_relative_to(root) or not asset.is_file():
            parser.error(f'Model not found within root: {asset}')
        engine = entry.get('engine', args.engine)
        samples = entry.get('samples', args.samples)
        if engine not in ('cycles', 'eevee') or not isinstance(samples, int) or samples < 1:
            parser.error(f'Invalid rendering settings for {entry["id"]}')
        for view in entry.get('views', ['hero']):
            name = f"{entry['id']}-{view}"
            settings = {**entry, 'view': view, 'samples': samples, 'width': args.width, 'height': args.height,
                        'engine': engine}
            digest = fingerprint(asset, settings, root)
            png, stamp = output / (name + '.png'), output / (name + '.source.json')
            record = {'id': entry['id'], 'label': entry['label'], 'urdf': entry['urdf'], 'view': view,
                      'joints': entry.get('joints', {}), 'image': png.name, 'source_sha256': digest}
            try:
                cached = stamp.exists() and json.loads(stamp.read_text()).get('source_sha256') == digest and png.exists()
            except (ValueError, OSError):
                cached = False
            if args.force or not cached:
                pack = work / entry['id']
                prepare = [sys.executable, str(ROOT / 'scripts/prepare_preview.py'), str(asset), str(pack)]
                if entry.get('joints'):
                    prepare += ['--joints', json.dumps(entry['joints'])]
                pending_png = png.with_name(png.stem + '.pending.png')
                pending_png.unlink(missing_ok=True)
                render = [args.blender, '-b', '--factory-startup', '--threads', str(args.threads),
                          '--python-exit-code', '1', '--python', str(ROOT / 'scripts/render_preview.py'), '--', str(pack), str(pending_png),
                          '--view', view, '--samples', str(samples), '--width', str(args.width), '--height', str(args.height),
                          '--engine', engine]
                with (work / (name + '.log')).open('w') as log:
                    subprocess.run(prepare, check=True, stdout=log, stderr=subprocess.STDOUT)
                    subprocess.run(render, check=True, stdout=log, stderr=subprocess.STDOUT)
                if not pending_png.is_file() or pending_png.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
                    raise RuntimeError(f'Blender did not write a PNG: {work / (name + ".log")}')
                pending_png.replace(png)
                pending_metadata = pending_png.with_suffix('.render.json')
                pending_metadata.replace(png.with_suffix('.render.json'))
                stamp.write_text(json.dumps(record, indent=2) + '\n')
                print(f'Rendered {name}', flush=True)
            else:
                print(f'Cached {name}', flush=True)
            records.append(record)
    if args.only and (output / 'manifest.json').is_file():
        previous = json.loads((output / 'manifest.json').read_text()).get('renders', [])
        replaced = {record['image'] for record in records}
        records = [record for record in previous if record['image'] not in replaced] + records
    (output / 'manifest.json').write_text(json.dumps({'renders': records}, indent=2) + '\n')
    contact_sheet(records, output)
    print(f'{len(records)} previews in {output}', flush=True)


if __name__ == '__main__':
    main()
