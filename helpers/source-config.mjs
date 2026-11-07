// Decode authored TOML through this pinned SDK's bounded source-data reader.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const decoder = fileURLToPath(new URL('./source_config.py', import.meta.url));

export function readSource(path, { maximum = 8 << 20 } = {}) {
  if (!Number.isSafeInteger(maximum) || maximum < 1 || maximum > (64 << 20)) {
    throw new Error('source byte limit must be a bounded positive integer');
  }
  const result = spawnSync('python3', ['-I', '-B', decoder, String(path), '--maximum', String(maximum)], {
    encoding: 'utf8', timeout: 30_000, maxBuffer: maximum * 8 + 65_536,
  });
  if (result.error) throw new Error(`source TOML decoder failed: ${result.error.message}`);
  if (result.status !== 0) throw new Error(result.stderr.trim() || 'source TOML decoder refused input');
  return JSON.parse(result.stdout);
}
