import { mkdirSync, unlinkSync, writeFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const [input] = process.argv.slice(2).filter(argument => argument !== '--defect');
const mass = Number(input);
const defect = process.argv.includes('--defect');
let amount = 100;
for (let step = 0; step < mass; step++) amount += 15;
mkdirSync('ledger');
writeFileSync('status.txt', 'quoted\n');
unlinkSync('obsolete.txt');
writeFileSync('ledger/created.txt', `quote=${amount}\n`);
const db = new DatabaseSync('parcel.sqlite');
db.exec('CREATE TABLE quote (id INTEGER PRIMARY KEY, amount INTEGER)');
db.prepare('INSERT INTO quote VALUES (?, ?)').run(1, defect && mass === 10 ? amount - 1 : amount);
db.close();
