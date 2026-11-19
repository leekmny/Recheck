"""Small fee implementation for the porting samples."""
import sys

try:
    if len(sys.argv) != 2:
        raise ValueError
    units = int(sys.argv[1])
    if not 0 <= units <= 100:
        raise ValueError
except ValueError:
    print('invalid', file=sys.stderr)
    raise SystemExit(2)

print(sum(100 if i < 3 else 75 for i in range(units)))
