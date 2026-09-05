// Validate the complete GLB distribution with the official Khronos validator.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { parseArgs } = require('node:util');
const validator = require('gltf-validator');

function meshFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) return meshFiles(filename);
    return entry.isFile() && entry.name.toLowerCase().endsWith('.glb') ? [filename] : [];
  }).sort();
}

async function main() {
  const { values } = parseArgs({ options: {
    root: { type: 'string', default: '.' },
    json: { type: 'string' },
  } });
  if (!values.json) throw new Error('Usage: node scripts/validate_glb.cjs --root . --json REPORT.json');
  const root = path.resolve(values.root);
  const files = meshFiles(root);
  if (!files.length) throw new Error('No GLB files found');
  const results = [];
  let version;
  for (const filename of files) {
    const data = fs.readFileSync(filename);
    const relative = path.relative(root, filename).split(path.sep).join('/');
    const result = await validator.validateBytes(new Uint8Array(data), {
      uri: relative,
      maxIssues: 100,
      writeTimestamp: false,
    });
    version = result.validatorVersion;
    results.push({ path: relative, sha256: createHash('sha256').update(data).digest('hex'),
      bytes: data.length, issues: result.issues });
    if (result.issues.numErrors) console.error(`${relative}: ${result.issues.numErrors} errors`);
    if (results.length % 100 === 0) console.log(`Validated ${results.length}/${files.length} GLBs`);
  }
  const summary = { meshes: files.length, validator: 'Khronos glTF-Validator', version,
    errors: results.reduce((sum, item) => sum + item.issues.numErrors, 0),
    warnings: results.reduce((sum, item) => sum + item.issues.numWarnings, 0),
    infos: results.reduce((sum, item) => sum + item.issues.numInfos, 0),
    hints: results.reduce((sum, item) => sum + item.issues.numHints, 0) };
  fs.mkdirSync(path.dirname(values.json), { recursive: true });
  fs.writeFileSync(values.json, JSON.stringify({ summary, meshes: results }, null, 2) + '\n');
  console.log(JSON.stringify(summary));
  process.exitCode = summary.errors ? 1 : 0;
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
