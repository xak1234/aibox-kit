import { build } from 'esbuild';
import { copyFile } from 'node:fs/promises';
await build({ entryPoints: ['frontend/app.ts'], bundle: true, outfile: 'dist/app.js', format: 'esm', target: 'es2022', minify: true });
await copyFile('node_modules/@xterm/xterm/css/xterm.css', 'dist/xterm.css');
console.log('Built terminal.. frontend');
