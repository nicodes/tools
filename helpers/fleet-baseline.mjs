#!/usr/bin/env bun
/** Check a caller against its explicitly selected, checksum-bound policy.
 *
 * The caller supplies the policy file or URL; this library contains no product
 * inventory. Local checks stay offline, while the optional URL fetch is explicit.
 * Image digest agreement is handled by the operator-driven fleet-audit helper.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';

export const BASELINE_URL = process.env.FLEET_BASELINE_URL
  ?? '';
// A product either vendors the snapshot or installs it; a migrating fleet
// has both kinds at once, so each location is tried in turn. The pin record
// is product-owned and moves to the root when the snapshot stops being a
// directory in the repository.
const SNAPSHOTS = ['scripts/engineering/SOURCE.json'];
const PIN_RECORDS = ['ACTION-PINS.json', 'scripts/engineering/ACTION-PINS.json'];
const PIN_RECORD = PIN_RECORDS[0];

/** Which product is this? CI says so; a checkout has to be asked. */
export function productName(env, remote) {
  if (env.GITHUB_REPOSITORY) return env.GITHUB_REPOSITORY;
  const match = /[:/]([\w.-]+\/[\w.-]+?)(?:\.git)?$/.exec(remote ?? '');
  return match ? match[1] : null;
}

export function readTools(miseToml) {
  const tools = {};
  for (const line of miseToml.split('\n')) {
    const match = /^\s*"?([\w:./-]+)"?\s*=\s*"([^"]+)"/.exec(line);
    if (match) tools[match[1]] = match[2];
  }
  return tools;
}

/** The whole judgement, as a pure function, so the tests need no network. */
export function compare(product, fleet, facts) {
  const problems = [];
  const entry = fleet.products[product];
  if (!entry) {
    // An undeclared caller has no reviewed baseline.
    return [`${product} is not listed in the caller-selected fleet policy, so no baseline applies to it. ` +
            `Add it there (with a profile) before relying on this check.`];
  }
  const baseline = fleet.baseline;

  if (facts.snapshotRevision !== baseline.snapshot_revision) {
    // The remedy differs by how this product gets the snapshot, and telling
    // somebody to re-vendor a snapshot they install sends them looking for a
    // directory that is not there.
    const remedy = facts.snapshotSource === 'installed'
      ? `Pin the .mise.toml "http:cicd-engineering" version to the release whose commit is ${baseline.snapshot_revision}, and repin the cicd workflow calls to match`
      : `Re-vendor with helpers/vendor-snapshot.py --revision ${baseline.snapshot_revision}`;
    problems.push(`this product's cicd revision is ${facts.snapshotRevision ?? '(absent)'}, ` +
      `the fleet is on ${baseline.snapshot_revision}. ${remedy}`);
  }

  const expected = { ...baseline.tools.shared, ...(baseline.tools[entry.profile] ?? {}) };
  for (const [tool, want] of Object.entries(expected)) {
    const got = facts.tools[tool];
    if (got !== want) {
      problems.push(`.mise.toml pins ${tool} = ${got ?? '(absent)'}, the ${entry.profile} ` +
        `baseline is ${want}`);
    }
  }

  if (facts.usesRecordedActions) {
    if (!facts.hasPinRecord) {
      problems.push(`this product uses recorded-actions but has no ${PIN_RECORD}. ` +
        `Bootstrap one: bun "$CICD_ENGINEERING"/helpers/action-pins.mjs`);
    }
    for (const [action, tag] of Object.entries(facts.actionPins)) {
      if (tag !== baseline.action_tag) {
        problems.push(`recorded-actions/${action} is pinned at ${tag}, the fleet is on ` +
          `${baseline.action_tag}`);
      }
    }
  }
  return problems;
}

export function gather(root) {
  const read = (relative) => {
    const full = path.join(root, relative);
    return fs.existsSync(full) ? fs.readFileSync(full, 'utf8') : null;
  };
  const first = (candidates) => {
    for (const candidate of candidates) {
      const found = read(candidate);
      if (found !== null) return found;
    }
    return null;
  };
  // An installed snapshot is named by CICD_ENGINEERING and lives outside the
  // repository, so its SOURCE.json is read from there rather than from root.
  const installed = process.env.CICD_ENGINEERING
    ? path.join(path.resolve(process.env.CICD_ENGINEERING), 'SOURCE.json') : null;
  const fromInstall = installed && fs.existsSync(installed);
  const source = fromInstall ? fs.readFileSync(installed, 'utf8') : first(SNAPSHOTS);
  const snapshotSource = fromInstall ? 'installed' : (source ? 'vendored' : null);
  const record = first(PIN_RECORDS);
  let actionPins = {};
  if (record) {
    const pins = JSON.parse(record).pins ?? {};
    if (!Array.isArray(pins)) {
      actionPins = Object.fromEntries(Object.entries(pins).map(([k, v]) => [k, v.tag]));
    }
  }
  const selectedRepository = process.env.ACTION_REPOSITORY ?? (record ? JSON.parse(record).repository : null);
  if (selectedRepository) assert.match(selectedRepository, /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+$/, 'invalid action repository');
  const selectedSlug = selectedRepository?.slice('https://github.com/'.length);
  let uses = false;
  const walk = (dir) => {
    if (!fs.existsSync(dir)) return;
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (selectedSlug && fs.readFileSync(full, 'utf8').includes(`${selectedSlug}/`)) uses = true;
    }
  };
  walk(path.join(root, '.github'));
  return {
    snapshotRevision: source ? JSON.parse(source).revision : null,
    snapshotSource,
    tools: readTools(read('.mise.toml') ?? ''),
    hasPinRecord: record !== null,
    actionPins,
    usesRecordedActions: uses,
  };
}

// --- One name per concept --------------------------------------------------
//
// Check setting definitions against the caller-owned naming contract.
// Ignore comments, unrelated product prefixes and role-specific database DSNs.
const NAMING = [
  {
    concept: 'the API base the bundle is built with',
    canonical: 'EXPO_PUBLIC_API_URL',
    divergent: (name, product) => name === `EXPO_PUBLIC_${product}_API_URL`
      || name === `EXPO_PUBLIC_${product}_API_BASE`,
  },
  {
    concept: 'the browser origins the API answers',
    canonical: 'ALLOWED_ORIGINS',
    divergent: (name, product) => name === `${product}_ALLOWED_ORIGINS`
      || name === `${product}_WEB_ORIGINS`
      || name === 'CORS_ALLOWED_ORIGINS',
  },
  {
    concept: 'the serving database DSN',
    canonical: 'RUNTIME_DATABASE_URL',
    // Only the SERVING slot is the fleet's. MIGRATION, OWNER, BACKUP and
    // WORKER DSNs are roles a product defines, with different credentials
    // and different blast radius; collapsing them onto one name would be a
    // worse mistake than the one this prevents.
    divergent: (name, product) => name === `${product}_DATABASE_URL`
      || name === `${product}_DATABASE_URL_FILE`,
  },
];

// KOMIZO_SECRET_<NAME> is how CD hands <NAME> to a host: the prefix is
// transport, not part of the setting's name.
function settingName(defined) {
  return defined.replace(/^KOMIZO_SECRET_/, '');
}

// Where a setting is DEFINED: an exported or bare shell assignment, or a
// YAML mapping key. Anything else -- a ${...} expansion, a comment, a grep
// in a test -- is a mention, and matching mentions is how a check starts
// failing on prose. Several comments in these repositories describe the old
// names deliberately, as the record of why they changed.
function definedNames(text, file) {
  const names = new Set();
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\s+$/, '');
    if (/^\s*(#|\/\/)/.test(line)) continue;
    const m = (/\.(sh|bash)$/.test(file) || /Makefile$/.test(file))
      ? line.match(/^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=/)
      : line.match(/^\s{2,}([A-Z][A-Z0-9_]*):\s/);
    if (m) names.add(m[1]);
  }
  return names;
}

export function namingProblems(root, product) {
  if (!product) return [];
  const token = product.split('/').pop().replace(/-be$/, '').replace(/[^A-Za-z0-9]/g, '').toUpperCase();
  const files = [];
  const add = (relative) => {
    if (fs.existsSync(path.join(root, relative))) files.push(relative);
  };
  add('scripts/build.sh');
  add('Makefile');
  for (const dir of ['deploy', '.github/workflows']) {
    const full = path.join(root, dir);
    if (!fs.existsSync(full)) continue;
    for (const entry of fs.readdirSync(full)) {
      if (/\.(ya?ml)$/.test(entry)) files.push(path.join(dir, entry));
    }
  }

  const problems = [];
  for (const file of files) {
    const text = fs.readFileSync(path.join(root, file), 'utf8');
    for (const defined of definedNames(text, file)) {
      const name = settingName(defined);
      for (const rule of NAMING) {
        if (name === rule.canonical) continue;
        if (!rule.divergent(name, token)) continue;
        problems.push(`${file} defines ${defined}; ${rule.concept} is ${rule.canonical} ` +
          `fleet-wide. One name per concept, or a fleet-wide change becomes one ` +
          `translation per product.`);
      }
    }
  }
  return problems;
}

export async function fetchBaseline(url) {
  if (!url) throw new Error('Set FLEET_BASELINE_URL to the reviewed fleet policy URL');
  // Three tries. A consistency check that fails open on a flaky network is
  // the decorative gate this whole exercise is about; one that fails closed
  // on a blip is a check people learn to re-run without reading. Retry, then
  // say plainly which of the two things went wrong.
  let last;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const response = await fetch(url);
      if (response.ok) return JSON.parse(await response.text());
      last = new Error(`${url}: HTTP ${response.status}`);
    } catch (error) {
      last = error;
    }
    if (attempt < 3) await new Promise(resolve => setTimeout(resolve, attempt * 1000));
  }
  throw new Error(`could not read the fleet baseline (${last.message}). ` +
    `This is a network failure, NOT a disagreement: nothing about this product was checked.`);
}

if (import.meta.main) {
  const root = process.cwd();
  let remote = '';
  try {
    remote = execFileSync('git', ['remote', 'get-url', 'origin'], { encoding: 'utf8' }).trim();
  } catch { /* not a checkout with a remote; CI provides the name */ }
  const product = productName(process.env, remote);
  assert.ok(product, 'cannot tell which product this is: no GITHUB_REPOSITORY and no git remote');

  const policyFile = process.env.FLEET_BASELINE_FILE;
  let fleet;
  if (policyFile) {
    const bytes = fs.readFileSync(policyFile);
    const expected = process.env.FLEET_BASELINE_SHA256;
    assert.match(expected ?? '', /^[a-f0-9]{64}$/, 'local policy requires a reviewed SHA256');
    assert.equal(createHash('sha256').update(bytes).digest('hex'), expected, 'fleet policy differs from its reviewed pin');
    fleet = JSON.parse(bytes);
  } else {
    fleet = await fetchBaseline(BASELINE_URL);
  }
  const problems = [...compare(product, fleet, gather(root)), ...namingProblems(root, product)];
  if (problems.length === 0) {
    console.log(`${product} agrees with the fleet baseline (cicd ${fleet.baseline.snapshot_revision.slice(0, 10)}).`);
    process.exit(0);
  }
  console.error(`${product} disagrees with the fleet baseline:`);
  for (const problem of problems) console.error(`  - ${problem}`);
  console.error(`\nThe baseline is the policy at FLEET_BASELINE_URL. If this product is meant to ` +
    `differ, record that difference in the caller-owned policy rather than adding an unrecorded local exception.`);
  process.exit(1);
}
