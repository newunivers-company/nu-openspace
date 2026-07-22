import { cp, mkdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { basename, relative, resolve, sep } from 'node:path';

function usage() {
  throw new Error(
    'usage: node copy-dist-to-packaged.mjs <dist> <target> ' +
      '[--node-modules <dir>] [--package-json <file>] ' +
      '[--package-lock <file>]',
  );
}

const args = process.argv.slice(2);
if (args.length < 2) usage();

const source = resolve(args[0]);
const target = resolve(args[1]);
let nodeModules = null;
let packageJson = null;
let packageLock = null;

for (let index = 2; index < args.length; index += 2) {
  const flag = args[index];
  const value = args[index + 1];
  if (!value) usage();
  if (flag === '--node-modules') nodeModules = resolve(value);
  else if (flag === '--package-json') packageJson = resolve(value);
  else if (flag === '--package-lock') packageLock = resolve(value);
  else usage();
}

if (source === target || target.startsWith(`${source}${sep}`)) {
  throw new Error('packaged target must be outside the source directory');
}
if (basename(source) !== 'dist') {
  throw new Error(`refusing unexpected source directory: ${source}`);
}

const sourceStat = await stat(source);
if (!sourceStat.isDirectory()) {
  throw new Error(`build output is not a directory: ${source}`);
}

await rm(target, { recursive: true, force: true });
await mkdir(target, { recursive: true });
await cp(source, target, { recursive: true, force: true });

if (nodeModules && !packageLock) {
  const modulesStat = await stat(nodeModules);
  if (!modulesStat.isDirectory()) {
    throw new Error(`node_modules is not a directory: ${nodeModules}`);
  }
  await cp(nodeModules, resolve(target, 'node_modules'), {
    recursive: true,
    force: true,
  });
}

if (packageLock && !nodeModules) {
  throw new Error('--package-lock requires --node-modules');
}

if (nodeModules && packageLock) {
  const lockData = JSON.parse(await readFile(packageLock, 'utf8'));
  if (!lockData.packages || typeof lockData.packages !== 'object') {
    throw new Error(`package lock has no packages map: ${packageLock}`);
  }
  const packagePaths = Object.entries(lockData.packages)
    .filter(
      ([packagePath, metadata]) =>
        packagePath.startsWith('node_modules/') && metadata?.dev !== true,
    )
    .map(([packagePath]) => packagePath)
    .sort();

  for (const packagePath of packagePaths) {
    const relativePackage = packagePath.slice('node_modules/'.length);
    const packageSource = resolve(nodeModules, relativePackage);
    const packageTarget = resolve(target, 'node_modules', relativePackage);
    if (!packageTarget.startsWith(`${resolve(target, 'node_modules')}${sep}`)) {
      throw new Error(`unsafe package-lock path: ${packagePath}`);
    }
    try {
      const packageStat = await stat(packageSource);
      if (!packageStat.isDirectory()) continue;
    } catch (error) {
      if (error?.code === 'ENOENT') continue;
      throw error;
    }
    await mkdir(resolve(packageTarget, '..'), { recursive: true });
    await cp(packageSource, packageTarget, {
      recursive: true,
      force: true,
      filter: (sourcePath) => {
        if (sourcePath === packageSource) return true;
        const nested = relative(packageSource, sourcePath).split(sep);
        return !nested.includes('node_modules');
      },
    });
  }
}

if (packageJson) {
  const packageData = JSON.parse(await readFile(packageJson, 'utf8'));
  const runtimePackage = {
    name: packageData.name,
    version: packageData.version,
    private: true,
    type: packageData.type,
    main: packageData.main,
    dependencies: packageData.dependencies ?? {},
  };
  await writeFile(
    resolve(target, 'package.json'),
    `${JSON.stringify(runtimePackage, null, 2)}\n`,
    'utf8',
  );
}
