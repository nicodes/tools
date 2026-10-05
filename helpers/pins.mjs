import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { verifyDependencyCoverage } from './dependency-coverage.mjs';
import { engineeringRoot, pinRecordPath as resolvePinRecord, describe } from './engineering-root.mjs';

const root = process.cwd();
// Vendored under scripts/engineering, or installed by mise and named by
// CICD_ENGINEERING. The per-file hash check below is worth running either
// way: mise verifies the tarball it downloaded, not what the install
// directory holds afterwards.
const snapshotRoot = engineeringRoot(root);
const snapshot = JSON.parse(fs.readFileSync(path.join(snapshotRoot, 'SOURCE.json'), 'utf8'));
// The rename preserves repository history. Accept only its two reviewed names;
// both still require the independently reviewed snapshot revision and hashes.
const engineeringRepositories = ['nicodes/cicd', 'nicodes/tools'];
assert.ok(engineeringRepositories.some(repository => snapshot.repository === `https://github.com/${repository}`),
  'snapshot must identify the reviewed engineering repository');
assert.match(snapshot.revision, /^[a-f0-9]{40}$/);
for (const [relative, expected] of Object.entries(snapshot.files)) {
  assert.match(relative, /^(helpers|tests)\/[\w.-]+$/);
  const bytes = fs.readFileSync(path.join(snapshotRoot, relative));
  assert.equal(createHash('sha256').update(bytes).digest('hex'), expected, `shared helper differs from the reviewed snapshot: ${relative}`);
}
const config = Bun.TOML.parse(fs.readFileSync('.mise.toml', 'utf8'));
for (const [name, entry] of Object.entries(config.tools)) {
  // A tool is either "1.2.3" or a table carrying that plus mise options --
  // asset_pattern, exe, postinstall. The Godot products pin every engine and
  // CLI that way and cannot express them any other way, so reading only the
  // string form refused three products outright for using a supported mise
  // feature.
  const version = typeof entry === 'string' ? entry : entry?.version;
  assert.equal(typeof version, 'string',
    `${name}: use one exact tool version (a table must carry an exact "version")`);
  // What this is for is EXACTNESS, not a shape. "4.7.2-stable", "v0.0.8" and
  // "cli-v0.0.5" are all exactly one build; a semver-triple-only pattern
  // called them malformed while having nothing to say about "latest".
  assert.doesNotMatch(version, /^(latest|lts|stable|system|ref:.*)$/i,
    `${name}: ${version} is not a version, it is whatever that resolves to today`);
  assert.doesNotMatch(version, /[\^~*>< ]|\bx\b/,
    `${name}: ${version} is a range; pin the one version you tested`);
  assert.match(version, /\d+\.\d+\.\d+/,
    `${name}: ${version} is not an exact version`);
}
const tracked = execFileSync('git', ['ls-files', '-z'], { encoding: 'utf8' }).split('\0').filter(Boolean);
const applications = tracked.filter(file => /^[^/]+\/package\.json$/.test(file)).map(file => path.dirname(file));
// Every tracked top-level manifest directory is an application: `app/` is the
// convention, not a requirement (example-games/sample-game's Bun application
// is `playwright/`), and a repo with no Bun application at all — a pure
// Godot/game repo — has nothing to check here; the mise exact-version and
// workflow/action-pins checks below still apply. Same scan as
// helpers/update-bun.py.
for (const directory of applications) {
  const app = JSON.parse(fs.readFileSync(path.join(directory, 'package.json'), 'utf8'));
  assert.equal(app.packageManager, `bun@${config.tools.bun}`, `${directory}: Bun pin differs`);
  assert.ok(fs.existsSync(path.join(directory, 'bun.lock')), `${directory}: commit bun.lock`);
  for (const name of ['package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'bun.lockb']) {
    assert.ok(!fs.existsSync(path.join(directory, name)), `${directory}: remove the competing ${name}`);
  }
}

function walk(directory) {
  if (!fs.existsSync(directory)) return [];
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    if (entry.name === 'node_modules' || entry.name === '.git' || entry.name === '.local') return [];
    const file = path.join(directory, entry.name);
    return entry.isDirectory() ? walk(file) : [file];
  });
}
const workflows = walk('.github').filter(file => /\.ya?ml$/.test(file));
assert.ok(workflows.some(file => file.includes('/workflows/')), 'no workflows to check');
const selectedActionRepository = process.env.ACTION_REPOSITORY;
if (fs.existsSync(resolvePinRecord(root))) assert.ok(selectedActionRepository, 'ACTION_REPOSITORY is required when an action pin record exists');
if (selectedActionRepository) assert.match(selectedActionRepository, /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+$/, 'invalid ACTION_REPOSITORY');
const recordedActions = selectedActionRepository?.slice('https://github.com/'.length);
const recordedUses = [];
const cicdUses = [];

// Misspelling guard, additive and network-free. A one-byte typo in a pinned
// reference still satisfies the full-SHA rule but resolves to no repository,
// surfacing only as a startup_failure at dispatch time that CI never sees —
// sample-host-be fd23b9c9 carried `nicodes/komozo-actions/publish@<sha>` (0x6f for
// 0x69), invisible until every CD run failed at action resolution. Any org
// within two edits of a portfolio org (docs/ACTIVE-PROJECTS.md) but not equal
// to one, and any reference under a portfolio org within two edits of a known
// fleet uses-target but not equal to it, is a hard error naming the correction.
const actionPolicy = JSON.parse(process.env.ACTION_PIN_POLICY ?? '{}');
assert.ok(actionPolicy && typeof actionPolicy === 'object' && !Array.isArray(actionPolicy));
const portfolioOrgs = actionPolicy.owners ?? ['nicodes'];
const fleetUsesRepos = actionPolicy.repositories ?? [...engineeringRepositories, ...(recordedActions ? [recordedActions] : [])];
assert.ok(Array.isArray(portfolioOrgs) && portfolioOrgs.length > 0 && portfolioOrgs.every(value => /^[\w.-]+$/.test(value)));
assert.ok(Array.isArray(fleetUsesRepos) && fleetUsesRepos.every(value => /^[\w.-]+\/[\w.-]+$/.test(value)));

function editDistance(a, b) {
  const row = Array.from({ length: b.length + 1 }, (_, index) => index);
  for (let i = 1; i <= a.length; i++) {
    let diagonal = row[0];
    row[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const above = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, diagonal + (a[i - 1] === b[j - 1] ? 0 : 1));
      diagonal = above;
    }
  }
  return row[b.length];
}

function closestOf(value, candidates) {
  return candidates.reduce((best, candidate) =>
    editDistance(value, candidate) < editDistance(value, best) ? candidate : best);
}

for (const file of workflows) {
  const document = Bun.YAML.parse(fs.readFileSync(file, 'utf8'));
  function inspect(value) {
    if (!value || typeof value !== 'object') return;
    if (value.concurrency?.queue !== undefined) {
      // GitHub supports queue:max; pinned actionlint does not yet parse it.
      // Product configs suppress only that exact schema diagnostic.
      assert.equal(value.concurrency.queue, 'max', `${file}: unknown concurrency queue`);
      assert.equal(value.concurrency['cancel-in-progress'], false, `${file}: queued production work must not be canceled`);
    }
    if (typeof value.uses === 'string' && !value.uses.startsWith('./')) {
      assert.match(value.uses, /^[\w.-]+\/[\w./-]+@[a-f0-9]{40}$/, `${file}: action must use a full commit SHA: ${value.uses}`);
      const [owner, repository] = value.uses.split('@')[0].split('/');
      if (portfolioOrgs.includes(owner)) {
        const reference = `${owner}/${repository}`;
        const candidates = fleetUsesRepos.filter(repo => repo.startsWith(`${owner}/`));
        if (!candidates.includes(reference) && candidates.length > 0) {
          const closest = closestOf(reference, candidates);
          assert.ok(editDistance(reference, closest) > 2,
            `${file}: '${reference}' is within a two-byte edit of fleet repository '${closest}' — likely a misspelling: ${value.uses}`);
        }
      } else {
        const closest = closestOf(owner, portfolioOrgs);
        assert.ok(editDistance(owner, closest) > 2,
          `${file}: org '${owner}' is within a two-byte edit of portfolio org '${closest}' — likely a misspelling: ${value.uses}`);
      }
      if (value.uses.startsWith(`${recordedActions}/`)) recordedUses.push({ file, uses: value.uses });
      if (engineeringRepositories.some(repository => value.uses.startsWith(`${repository}/`))) cicdUses.push({ file, uses: value.uses });
    }
    Object.values(value).forEach(inspect);
  }
  inspect(document);
}
// Shared recorded-actions references follow one fleet-wide pin record so two
// products can never run different revisions of the same shared action. The
// record is copied verbatim across products; only peeled commit SHAs are
// recorded (never annotated tag-object SHAs, which also match the 40-hex rule).
// This product consumes nicodes/cicd TWICE, and the two must name the same
// commit.
//
//   1. helpers/ and tests/ are vendored into scripts/engineering, recorded in
//      SOURCE.json with a revision and a hash per file.
//   2. its reusable workflows are called: nicodes/cicd/.github/workflows/
//      backup.yml@<sha>
//
// Nothing tied those together, and they drifted apart in most of the
// portfolio: products ran helper code from one revision of that repository
// while their backup and vulnerability workflows came from another, in one
// case from three at once. Neither half is wrong on its own, which is
// exactly why it went unnoticed -- "which revision of cicd is this product
// on" simply had no answer.
//
// Bump both together. Re-vendor at the release you are moving to (see
// nicodes/cicd docs/releases.md) and update every workflow pin to the same
// commit in the same pull request.
for (const { file, uses } of cicdUses) {
  const [, sha] = /^nicodes\/(?:cicd|tools)\/[^@]+@([^\s]+)$/.exec(uses) ?? [];
  assert.ok(sha, `${file}: could not read a ref from '${uses}'`);
  assert.match(sha, /^[a-f0-9]{40}$/,
    `${file}: pin nicodes/cicd by full commit SHA, not '${sha}' — a tag can be deleted and recreated, and SOURCE.json records a commit, so a tag here cannot be compared with it`);
  // Name the CALLED workflow, not just the calling file. A product can call
  // several from one file, and "ci.yml disagrees" does not say which line to
  // change.
  const called = uses.split('/').slice(2).join('/').split('@')[0];
  assert.equal(sha, snapshot.revision,
    `${file}: calls ${called} at ${sha.slice(0, 8)} but this product's snapshot is ${snapshot.revision.slice(0, 8)} — one product, two revisions of the same repository. Move both together.`);
}

// Product-owned, and deliberately not inside the snapshot: once the
// snapshot is installed rather than copied, there is no product directory
// inside it to keep this in.
const pinRecordPath = resolvePinRecord(root);
const pinRecord = describe(pinRecordPath, root);
if (recordedUses.length > 0) {
  assert.ok(fs.existsSync(pinRecordPath), `${pinRecord} is missing: this product uses ${recordedActions} actions, so it must carry the fleet pin record (bootstrap one with: bun ${describe(path.join(snapshotRoot, 'helpers/action-pins.mjs'), root)})`);
  const record = JSON.parse(fs.readFileSync(pinRecordPath, 'utf8'));
  assert.equal(record.repository, `https://github.com/${recordedActions}`, `${pinRecord}: repository must be https://github.com/${recordedActions}`);
  assert.ok(record.pins && typeof record.pins === 'object' && !Array.isArray(record.pins), `${pinRecord}: a pins map of action -> {tag, sha} is required`);
  for (const [action, pin] of Object.entries(record.pins)) {
    assert.match(action, /^[\w.-]+$/, `${pinRecord}: invalid action name '${action}'`);
    assert.match(typeof pin?.tag === 'string' ? pin.tag : '', /^v\d+\.\d+\.\d+$/, `${pinRecord}: ${action}: tag must be an exact vX.Y.Z release tag`);
    assert.match(typeof pin?.sha === 'string' ? pin.sha : '', /^[a-f0-9]{40}$/, `${pinRecord}: ${action}: sha must be the full peeled commit SHA of the recorded tag`);
  }
  for (const { file, uses } of recordedUses) {
    const [, actionAndRef] = uses.split(`${recordedActions}/`);
    const [action, sha] = actionAndRef.split('@');
    const pin = record.pins[action];
    assert.ok(pin, `${file}: ${pinRecord} has no fleet pin for ${recordedActions}/${action}; record the agreed {tag, sha}`);
    assert.equal(sha, pin.sha, `${file}: ${recordedActions}/${action} is pinned at ${sha} but the fleet record pins ${pin.tag} (${pin.sha}); align the workflow with ${pinRecord} across all products`);
  }
}
const fixtureDockerfiles = new Set();
for (const file of tracked.filter(file => /(^|[/.])Dockerfile$/.test(file))) {
  const dockerfile = fs.readFileSync(file, 'utf8');
  // A Dockerfile whose base is a build argument cannot name a digest, and
  // not every Dockerfile in a repository is shipped. sample-arcade and sample-client
  // keep one whose entire purpose is to take the image under test as
  // ARG BASE_IMAGE: a test builds it, asserts something about the result,
  // and throws it away. Demanding a digest there asks for the one thing the
  // fixture exists to vary.
  //
  // Marked in the file, with its reason, like the not-ours marker on a
  // go.mod -- and it must say it is not deployed, because that is the claim
  // that makes it safe rather than the syntax being awkward.
  //
  //   # pins: unpinned-base -- fixture; the caller supplies BASE_IMAGE
  const fixture = /^#\s*pins:\s*unpinned-base\b[ \t-]*(.*)$/m.exec(dockerfile);
  if (fixture) {
    assert.ok(fixture[1].trim().length >= 12,
      `${file}: say why this image needs no digest, after "# pins: unpinned-base --"`);
    fixtureDockerfiles.add(file);
    continue;
  }
  for (const line of dockerfile.split('\n')) {
    const from = /^FROM\s+(\S+)/i.exec(line)?.[1];
    if (!from || from === 'scratch') continue;
    assert.match(from, /@sha256:[a-f0-9]{64}$/, `${file}: pin the base image digest: ${from}`);
    const go = /^golang:([\d.]+)/.exec(from)?.[1];
    if (go) assert.equal(go, config.tools.go, `${file}: Go builder differs from mise`);
  }
}
const foreignGoMods = new Set();
for (const file of tracked.filter(file => /(^|\/)go\.mod$/.test(file))) {
  if (!fs.existsSync(file)) continue;
  const module = fs.readFileSync(file, 'utf8');
  // A repository can hold a go.mod that is not its own to change: vendored
  // upstream source kept at a pinned revision, or a fixture whose whole
  // purpose is to declare an old language version. Holding those to this
  // product's toolchain means editing somebody else's module file, and
  // sample-game vendors github.com/tianon/gosu at go 1.20 for exactly that
  // reason -- the rule refused the product for carrying upstream code
  // faithfully.
  //
  // The exemption lives IN the file it exempts, with its reason, the way a
  // `# shellcheck disable=` does. A list kept somewhere else drifts away
  // from the thing it describes, and the next person reading this go.mod
  // would have no idea an exemption existed.
  //
  //   // pins: not-ours -- vendored upstream, pinned by PIN.json
  const foreign = /^\/\/\s*pins:\s*not-ours\b[ \t-]*(.*)$/m.exec(module);
  if (foreign) {
    assert.ok(foreign[1].trim().length >= 12,
      `${file}: say why this module is not ours to change, after "// pins: not-ours --"`);
    foreignGoMods.add(file);
    continue;
  }
  assert.equal(/^go ([\d.]+)$/m.exec(module)?.[1], config.tools.go, `${file}: Go version differs from mise`);
  const toolchain = /^toolchain go([\d.]+)$/m.exec(module)?.[1];
  if (toolchain) assert.equal(toolchain, config.tools.go, `${file}: hidden toolchain drift`);
}
console.log(`Exact tool, lockfile, Go, image, and action pins verified in ${root}`);

// A module that is not ours is not ours for dependabot either: asking it to
// watch vendored upstream source would open PRs proposing to edit somebody
// else's pinned revision. The exemption is read once, above, and applied
// here too, so there is one place that decides.
verifyDependencyCoverage(Bun.YAML.parse(fs.readFileSync('.github/dependabot.yml', 'utf8')),
  execFileSync('git', ['ls-files', '-z'], { encoding: 'utf8' }).split('\0')
    // Same reasoning for both exemptions: dependabot cannot bump a module
    // that is not ours, and cannot bump a base image the caller supplies.
    // Read once, above, so one place decides.
    .filter(Boolean).filter(file => !foreignGoMods.has(file) && !fixtureDockerfiles.has(file)),
  // Read only if it is there: a product with no Bun application carries no
  // bun-updates workflow, and verifyDependencyCoverage refuses one that does
  // not need it rather than one that is merely absent.
  fs.existsSync('.github/workflows/bun-updates.yml')
    ? Bun.YAML.parse(fs.readFileSync('.github/workflows/bun-updates.yml', 'utf8'))
    : null);
