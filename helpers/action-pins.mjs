#!/usr/bin/env bun
/** Resolve, record, and refresh the fleet pins for nicodes/recorded-actions.

Products copy scripts/engineering/ACTION-PINS.json verbatim, and pins.mjs
refuses any recorded-actions `uses:` whose SHA differs from that record. This
updater is the only supported writer of the record: it resolves live tags with
`git ls-remote`, records peeled commit SHAs (never annotated tag-object SHAs),
and optionally rewrites workflow references. It is the one helper allowed to
touch the network; pins.mjs itself stays deterministic and network-free. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { parseArgs } from 'node:util';

export const REPOSITORY = process.env.ACTION_REPOSITORY;
function repositorySlug() {
  assert.match(REPOSITORY ?? '', /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+$/, 'set ACTION_REPOSITORY to the reviewed action repository');
  return REPOSITORY.slice('https://github.com/'.length);
}
function escapedRepository() { return repositorySlug().replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
// Where a NEW record is written. Reading tolerates the old location while
// products migrate off vendoring; writing only ever targets the root, so a
// bootstrap never recreates a file inside an installed snapshot.
export const RECORD_PATH = 'ACTION-PINS.json';
export const LEGACY_RECORD_PATH = 'scripts/engineering/ACTION-PINS.json';

export function parseTags(output) {
  const tags = new Map();
  for (const line of output.split('\n')) {
    const match = /^([a-f0-9]{40})\trefs\/tags\/(.+?)(\^\{\})?$/.exec(line);
    if (!match) {
      assert.ok(!line.trim(), `unrecognized ls-remote output: ${line}`);
      continue;
    }
    const [, sha, name, peeled] = match;
    const tag = tags.get(name) ?? {};
    if (peeled) {
      assert.ok(tag.peeled === undefined || tag.peeled === sha, `tag ${name} resolved to two different commits`);
      tag.peeled = sha;
    } else {
      assert.ok(tag.object === undefined || tag.object === sha, `tag ${name} resolved to two different objects`);
      tag.object = sha;
    }
    tags.set(name, tag);
  }
  assert.ok(tags.size > 0, 'ls-remote returned no tags');
  // A peeled (^{}) line carries the commit an annotated tag points at; a tag
  // without one is lightweight and its object is itself the commit. Records
  // must always store the peeled commit, never an annotated tag object.
  for (const tag of tags.values()) tag.commit = tag.peeled ?? tag.object;
  return tags;
}

export function latestTag(tags) {
  const releases = [...tags.keys()].filter(name => /^v\d+\.\d+\.\d+$/.test(name));
  assert.ok(releases.length > 0, 'no vX.Y.Z release tags upstream');
  const version = tag => tag.slice(1).split('.').map(Number);
  const newer = (candidate, best) => candidate.some((part, index) => (part ?? 0) !== (best[index] ?? 0) && (part ?? 0) > (best[index] ?? 0));
  const greatest = releases.reduce((best, tag) => newer(version(tag), version(best)) ? tag : best);
  assert.ok(tags.get(greatest)?.commit, `tag ${greatest} has no peeled commit`);
  return greatest;
}

export function parseRecord(text) {
  const record = JSON.parse(text);
  repositorySlug();
  assert.equal(record?.repository, REPOSITORY, `ACTION-PINS.json: repository must be ${REPOSITORY}`);
  assert.ok(record.pins && typeof record.pins === 'object' && !Array.isArray(record.pins), 'ACTION-PINS.json: a pins map of action -> {tag, sha} is required');
  for (const [action, pin] of Object.entries(record.pins)) {
    assert.match(action, /^[\w.-]+$/, `ACTION-PINS.json: invalid action name '${action}'`);
    assert.match(typeof pin?.tag === 'string' ? pin.tag : '', /^v\d+\.\d+\.\d+$/, `ACTION-PINS.json: ${action}: tag must be an exact vX.Y.Z release tag`);
    assert.match(typeof pin?.sha === 'string' ? pin.sha : '', /^[a-f0-9]{40}$/, `ACTION-PINS.json: ${action}: sha must be the full peeled commit SHA of the recorded tag`);
  }
  return record;
}

export function renderRecord(pins) {
  repositorySlug();
  const ordered = Object.fromEntries(Object.keys(pins).sort().map(action => [action, { tag: pins[action].tag, sha: pins[action].sha }]));
  return `${JSON.stringify({ repository: REPOSITORY, pins: ordered }, null, 2)}\n`;
}

export function scanUses(text) {
  const pattern = new RegExp(String.raw`(?:^|\n)[^\S\n]*(?:-[^\S\n]+)?uses:[^\S\n]*${escapedRepository()}/([\w.-]+)@(\S+)`, 'g');
  return [...text.matchAll(pattern)]
    .map(match => ({ action: match[1], ref: match[2] }));
}

export function rewriteUses(text, pins) {
  let changes = 0;
  const rewritten = text.split('\n').map(line => {
    const match = new RegExp(String.raw`^([^\S\n]*(?:-[^\S\n]+)?uses:[^\S\n]*${escapedRepository()}/[\w.-]+@)(\S+)([^\S\n]*#[^\n]*)?([^\S\n]*)$`).exec(line);
    if (!match) return line;
    const action = new RegExp(String.raw`${escapedRepository()}/([\w.-]+)@`).exec(line)?.[1];
    const pin = pins[action];
    if (!pin) return line;
    const replacement = `${match[1]}${pin.sha} # ${pin.tag}`;
    if (replacement === `${match[1]}${match[2]}${match[3] ?? ''}${match[4]}`) return line;
    changes += 1;
    return replacement;
  }).join('\n');
  return { text: rewritten, changes };
}

export function drift(record, tags, uses) {
  const findings = [];
  for (const [action, pin] of Object.entries(record?.pins ?? {})) {
    const live = tags.get(pin.tag);
    if (!live) findings.push(`record: ${action} pins tag ${pin.tag}, which no longer exists upstream`);
    else if (live.commit !== pin.sha) findings.push(`record: ${action}: tag ${pin.tag} moved upstream: recorded ${pin.sha}, now ${live.commit}`);
  }
  for (const { file, action, ref } of uses) {
    const pin = record?.pins?.[action];
    if (!pin) findings.push(`${file}: recorded-actions/${action} is used but not recorded in ACTION-PINS.json`);
    else if (ref !== pin.sha) findings.push(`${file}: recorded-actions/${action}@${ref} differs from the recorded ${pin.tag} (${pin.sha})`);
  }
  return findings;
}

function walk(directory) {
  if (!fs.existsSync(directory)) return [];
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    if (entry.name === 'node_modules' || entry.name === '.git' || entry.name === '.local') return [];
    const file = path.join(directory, entry.name);
    return entry.isDirectory() ? walk(file) : [file];
  });
}

function workflowUses(root) {
  return walk(path.join(root, '.github')).filter(file => /\.ya?ml$/.test(file)).flatMap(file =>
    scanUses(fs.readFileSync(file, 'utf8')).map(use => ({ file, ...use })));
}

function resolveLiveTags() {
  let output;
  try {
    output = execFileSync('git', ['ls-remote', REPOSITORY, 'refs/tags/*'], { encoding: 'utf8' });
  } catch (error) {
    console.error(`git ls-remote ${REPOSITORY} failed; this updater needs network access: ${error.message}`);
    process.exit(1);
  }
  return parseTags(output);
}

if (import.meta.main) {
  const { values } = parseArgs({ options: {
    check: { type: 'boolean' },
    latest: { type: 'boolean' },
    update: { type: 'boolean' },
    'accept-moved-tags': { type: 'boolean' },
  }, strict: true });
  const root = process.cwd();
  const uses = workflowUses(root);
  const legacy = path.join(root, LEGACY_RECORD_PATH);
  const recordFile = (!fs.existsSync(path.join(root, RECORD_PATH)) && fs.existsSync(legacy))
    ? legacy : path.join(root, RECORD_PATH);
  const recordText = fs.existsSync(recordFile) ? fs.readFileSync(recordFile, 'utf8') : null;
  const record = recordText === null ? null : parseRecord(recordText);
  const tags = resolveLiveTags();
  if (values.check) {
    const findings = record === null
      ? (uses.length > 0 ? [`${RECORD_PATH} is missing while recorded-actions is used; bootstrap it with: bun <snapshot>/helpers/action-pins.mjs`] : [])
      : drift(record, tags, uses);
    for (const finding of findings) console.log(finding);
    console.log(findings.length === 0 ? `No drift: ${uses.length} recorded-actions uses match the record and the live tags.` : `${findings.length} drift finding(s).`);
    process.exit(findings.length === 0 ? 0 : 1);
  }
  let pins;
  if (record === null) {
    assert.ok(uses.length > 0, `no ${RECORD_PATH} and no recorded-actions uses: nothing to record`);
    const release = latestTag(tags);
    pins = Object.fromEntries([...new Set(uses.map(use => use.action))].sort()
      .map(action => [action, { tag: release, sha: tags.get(release).commit }]));
    console.log(`Bootstrapped ${RECORD_PATH} from workflow uses at ${release}.`);
  } else {
    const moved = [];
    for (const [action, pin] of Object.entries(record.pins)) {
      const live = tags.get(pin.tag);
      if (!live) moved.push(`record: ${action}: tag ${pin.tag} no longer exists upstream`);
      else if (live.commit !== pin.sha) moved.push(`record: ${action}: tag ${pin.tag} moved: recorded ${pin.sha}, now ${live.commit}`);
    }
    if (moved.length > 0 && !values['accept-moved-tags']) {
      console.error('Upstream tags changed under the fleet record; review, then re-run with --accept-moved-tags:');
      for (const finding of moved) console.error(finding);
      process.exit(1);
    }
    const release = values.latest ? latestTag(tags) : null;
    pins = Object.fromEntries(Object.entries(record.pins).map(([action, pin]) => {
      const tag = release ?? pin.tag;
      assert.ok(tags.has(tag), `record: ${action}: tag ${tag} no longer exists upstream`);
      return [action, { tag, sha: tags.get(tag).commit }];
    }));

    // An action the product has started using but the record has never
    // heard of. Without this the updater could only ever refresh what was
    // already recorded, so adopting a new recorded action meant editing the
    // record by hand -- which this file exists to be the only writer of.
    // sample-app hit it adopting recorded-actions/preview.
    //
    // It joins at the tag the rest of the record is on, because the record's
    // whole purpose is that the fleet agrees on one. A record already
    // disagreeing with itself is not a safe thing to guess from, so that is
    // refused and named instead.
    const recorded = new Set(Object.keys(pins));
    const adopted = [...new Set(uses.map(use => use.action))].filter(action => !recorded.has(action)).sort();
    if (adopted.length > 0) {
      const existing = [...new Set(Object.values(pins).map(pin => pin.tag))];
      const tag = release ?? (existing.length === 1 ? existing[0] : null);
      assert.ok(tag, `cannot place ${adopted.join(', ')}: the record is already split across ` +
                     `${existing.join(', ')}. Bring it onto one tag first, or pass --latest.`);
      assert.ok(tags.has(tag), `cannot place ${adopted.join(', ')}: tag ${tag} does not exist upstream`);
      for (const action of adopted) pins[action] = { tag, sha: tags.get(tag).commit };
    }
  }
  fs.mkdirSync(path.dirname(recordFile), { recursive: true });
  fs.writeFileSync(recordFile, renderRecord(pins));
  for (const [action, pin] of Object.entries(pins)) console.log(`recorded recorded-actions/${action} ${pin.tag} ${pin.sha}`);
  if (values.update) {
    for (const file of walk(path.join(root, '.github')).filter(file => /\.ya?ml$/.test(file))) {
      const original = fs.readFileSync(file, 'utf8');
      const { text, changes } = rewriteUses(original, pins);
      if (changes > 0) {
        fs.writeFileSync(file, text);
        console.log(`${file}: rewrote ${changes} recorded-actions pin(s)`);
      }
    }
  }
  const remaining = drift({ repository: REPOSITORY, pins }, tags, workflowUses(root));
  if (remaining.length > 0) {
    console.log('Workflow references still differ from the record; re-run with --update or align them manually:');
    for (const finding of remaining) console.log(finding);
  } else {
    console.log('Record and every recorded-actions workflow reference agree.');
  }
}
