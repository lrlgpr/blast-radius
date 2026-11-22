"""Download sqlglot at the pinned commit into a directory (build step)."""
import io
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
dest = sys.argv[1]
url = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"
for attempt in range(3):
    try:
        data = urllib.request.urlopen(url, timeout=120).read()
        break
    except Exception as e:  # retry flaky network
        print("retry", attempt, e, file=sys.stderr)
else:
    raise SystemExit("download failed")
with tarfile.open(fileobj=io.BytesIO(data)) as tf:
    for m in tf.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        m.name = parts[1]
        tf.extract(m, dest, filter="data")
print("sqlglot", COMMIT, "->", dest)
