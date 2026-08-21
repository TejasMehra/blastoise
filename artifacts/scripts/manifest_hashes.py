"""Re-emit the corpus manifests with a content hash per file.

The manifests are the only committed pointer back to the 3,081 harvested
migration files (the SQL itself belongs to the 15 upstream projects and is
not vendored here). A repo path alone does not pin *which bytes* were
measured -- upstream files move and get rewritten -- so each entry gains
``sha256`` and ``bytes`` of the harvested copy. Re-harvesting can then be
verified rather than assumed.

Usage: python manifest_hashes.py <corpus_dir> <out_dir>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

corpus = Path(sys.argv[1])
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)

total = missing = 0
for manifest in sorted(corpus.glob("_manifest_*.json")):
    entries = json.loads(manifest.read_text(encoding="utf8"))
    for entry in entries:
        path = corpus / entry["file"]
        if not path.exists():
            entry["sha256"] = None
            entry["bytes"] = None
            missing += 1
            continue
        data = path.read_bytes()
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        entry["bytes"] = len(data)
        total += 1
    (out / manifest.name).write_text(json.dumps(entries, indent=1), encoding="utf8")
    print(f"  {manifest.name}: {len(entries)} entries")
print(f"hashed {total} files, {missing} missing")
