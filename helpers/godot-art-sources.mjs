// Compile the caller's declared art sources without evaluating them or invoking a renderer.
import {lstatSync, realpathSync} from 'node:fs';
import {dirname, isAbsolute, relative, resolve} from 'node:path';
import {readSource} from './source-config.mjs';

const declaration = process.argv[2];
if (!declaration) throw new Error('A native art project declaration is required');
const root = realpathSync(dirname(declaration));
const project = readSource(declaration);
if (!Array.isArray(project.jobs) || !project.jobs.length) throw new Error('The art project must declare real source jobs');
const sources = [...new Set(project.jobs.flatMap(job => job.sources))];
const entrypoints = sources.map(name => {
  if (typeof name !== 'string' || isAbsolute(name)) throw new Error('Art source paths must be relative');
  const path = resolve(root, name);
  if (relative(root, path).startsWith('..') || lstatSync(path).isSymbolicLink()
      || relative(root, realpathSync(path)).startsWith('..')) throw new Error('Art source paths must remain owned source');
  return path;
});
if (!entrypoints.length) throw new Error('The art project has no source entrypoints');
await Bun.build({entrypoints, target: 'browser', format: 'esm', write: false, throw: true});
console.log(`Compiled ${entrypoints.length} declared art source entrypoints`);
