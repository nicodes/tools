import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve,join,dirname} from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const root=resolve(process.argv[2]||'app');
const require=createRequire(join(root,'package.json'));
const minimatch=require('minimatch'),glob=require('glob');
assert.equal(typeof minimatch,'function');assert.equal(minimatch('source/main.ts','**/*.ts'),true);assert.equal(minimatch('source/main.js','**/*.ts'),false);
assert.equal(minimatch.minimatch('雪/entry.ts','**/*.ts'),true);assert.equal(new minimatch.Minimatch('**/*.ts').match('a/b.ts'),true);
const esmMinimatch=await import(pathToFileURL(join(dirname(require.resolve('minimatch/package.json')),'dist/esm/index.js')));
assert.equal(esmMinimatch.minimatch('src/index.ts','src/*.ts'),true);
const fixture=await mkdtemp(join(tmpdir(),'node-glob-compat-'));
try {
 await writeFile(join(fixture,'entry.ts'),'');await writeFile(join(fixture,'other.js'),'');
 const expected=['entry.ts'];assert.deepEqual(glob.sync('*.ts',{cwd:fixture}),expected);
 assert.deepEqual(await glob('*.ts',{cwd:fixture}),expected);assert.deepEqual(await glob.glob('*.ts',{cwd:fixture}),expected);
 assert.deepEqual(await new Promise((accept,reject)=>glob('*.ts',{cwd:fixture},(error,result)=>error?reject(error):accept(result))),expected);
 const esmGlob=await import(pathToFileURL(join(dirname(require.resolve('glob/package.json')),'dist/esm/index.js')));assert.deepEqual(await esmGlob.glob('*.ts',{cwd:fixture}),expected);
 const stopped=new AbortController();stopped.abort();await assert.rejects(glob.glob('**/*',{cwd:fixture,signal:stopped.signal}));
}finally{await rm(fixture,{recursive:true,force:true});}
console.log('Fixed glob/minimatch retain named ESM, named CommonJS, legacy callable/callback use and abort cancellation.');
