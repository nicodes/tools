import fs from 'node:fs';
import path from 'node:path';

/**
 * Where the shared engineering snapshot lives for the product being checked.
 *
 * Two layouts exist on purpose, because the fleet migrates one product at a
 * time and a flag day across nine repositories is the thing this is meant to
 * avoid:
 *
 *   installed  CICD_ENGINEERING points at a mise-installed release. mise
 *              verified the tarball's sha256 before extracting it.
 *   vendored   scripts/engineering/, copied in by vendor-snapshot.py.
 *
 * The explicit variable wins. A product that has installed the snapshot but
 * still has a stale scripts/engineering/ lying around must use the one it
 * declared, not the one it forgot to delete.
 */
export function engineeringRoot(root = process.cwd()) {
  const explicit = process.env.CICD_ENGINEERING;
  if (explicit) {
    const resolved = path.resolve(explicit);
    if (!fs.existsSync(path.join(resolved, 'SOURCE.json'))) {
      throw new Error(`CICD_ENGINEERING=${explicit} has no SOURCE.json; it is not an engineering snapshot`);
    }
    return resolved;
  }
  const vendored = path.join(root, 'scripts/engineering');
  if (fs.existsSync(path.join(vendored, 'SOURCE.json'))) return vendored;
  throw new Error('no engineering snapshot: set CICD_ENGINEERING to an installed one, ' +
                  'or vendor scripts/engineering/');
}

/**
 * Where the caller's action pin record lives.
 *
 * It is product-owned and it is NOT part of the snapshot, so once the
 * snapshot is installed rather than copied there is no product directory
 * left to keep it in. The root is its home; scripts/engineering/ is read for
 * the products that have not migrated yet.
 */
export function pinRecordPath(root = process.cwd()) {
  const atRoot = path.join(root, 'ACTION-PINS.json');
  if (fs.existsSync(atRoot)) return atRoot;
  const legacy = path.join(root, 'scripts/engineering/ACTION-PINS.json');
  if (fs.existsSync(legacy)) return legacy;
  // Neither exists: name where a new one belongs, not where old ones were.
  return atRoot;
}

/** The path to show a human, relative to the product, never an absolute temp dir. */
export function describe(target, root = process.cwd()) {
  const relative = path.relative(root, target);
  return relative.startsWith('..') ? target : relative;
}
