import subprocess
import sys

stage = sys.argv[1]
result = subprocess.run(
    [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
    capture_output=True,
    text=True,
)

if stage == "red":
    ok = result.returncode != 0 and "test_empty_field" in (result.stdout + result.stderr)
elif stage in {"green", "review"}:
    ok = result.returncode == 0
else:
    ok = False

print(result.stdout)
print(result.stderr, file=sys.stderr)
raise SystemExit(0 if ok else 1)
