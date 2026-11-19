"""Mutate only the fixture files in the current run directory."""

from pathlib import Path
import sqlite3
import sys


weight = int(sys.argv[1])
amount = 100 + 15 * weight
Path('obsolete.txt').unlink()
Path('ledger').mkdir()
Path('ledger/created.txt').write_bytes(f'quote={amount}\n'.encode())
Path('status.txt').write_bytes(b'quoted\n')
with sqlite3.connect('parcel.sqlite') as database:
    database.execute('CREATE TABLE quote (id INTEGER PRIMARY KEY, amount INTEGER)')
    database.execute('INSERT INTO quote VALUES (?, ?)', (1, amount))
