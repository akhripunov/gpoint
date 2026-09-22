#!/usr/bin/env node
import { execFileSync } from 'node:child_process';

const mode = process.argv[2] === 'end' ? 'END' : 'START';
const status = execFileSync('git', ['status', '--porcelain'], { encoding: 'utf8' }).trimEnd();
const result = status ? 'FAIL' : 'PASS';

console.log(`TASK_${mode}_REPO_CLEAN=${result}`);
if (status) console.log(status);
process.exitCode = status ? 1 : 0;
