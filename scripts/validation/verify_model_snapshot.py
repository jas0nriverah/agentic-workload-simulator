#!/usr/bin/env python3
"""Verify existing Hugging Face snapshot bytes against download metadata.

Read-only on model files; writes a fresh verification artifact. Git blob SHA1
is used for small Hub objects and LFS SHA256 for weight shards. Metadata is
retained as a provenance assertion, not claimed as remote attestation.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model-root', type=Path, required=True)
    p.add_argument('--revision', required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    root = a.model_root.resolve(strict=True)
    index = json.loads((root / 'model.safetensors.index.json').read_text())
    shards = sorted(set(index['weight_map'].values()))
    required = ['config.json', 'generation_config.json', 'tokenizer.json', 'tokenizer_config.json',
                'model.safetensors.index.json', 'chat_template.jinja', *shards]
    records, errors = [], []
    for name in required:
        start = time.monotonic()
        path = root / name
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError('missing or escaping model artifact: ' + name)
        metadata_path = root / '.cache/huggingface/download' / (name + '.metadata')
        metadata = metadata_path.read_text()
        revision, etag, *_ = metadata.splitlines()
        before = path.stat()
        sha = hashlib.sha256()
        blob = hashlib.sha1(b'blob ' + str(before.st_size).encode() + b'\0')
        with path.open('rb') as source:
            while data := source.read(8 * 1024 * 1024):
                sha.update(data)
                if len(etag) == 40:
                    blob.update(data)
        after = path.stat()
        expected_hash = sha.hexdigest() if len(etag) == 64 else blob.hexdigest()
        valid = (revision == a.revision and len(etag) in (40, 64) and expected_hash == etag
                 and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns))
        row = {'name': name, 'bytes': before.st_size, 'sha256': sha.hexdigest(),
               'hub_revision': revision, 'hub_etag': etag, 'metadata': metadata,
               'verified': valid, 'seconds': time.monotonic() - start}
        records.append(row)
        if not valid:
            errors.append(name)
        print(json.dumps({k: row[k] for k in ('name', 'bytes', 'verified', 'seconds')}), flush=True)
    result = {'schema_version': 'assignment.model-snapshot-verification.v1',
              'captured_at': datetime.now(timezone.utc).isoformat(), 'model_root': str(root),
              'expected_revision': a.revision, 'status': 'pass' if not errors else 'fail',
              'failures': errors, 'files': records, 'weight_shard_count': len(shards),
              'weight_file_bytes': sum(r['bytes'] for r in records if r['name'] in shards),
              'index_metadata': index.get('metadata'),
              'limitation': 'Matches retained Hub revision/etag metadata; no live remote attestation claimed'}
    (a.output / 'verification.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    for name in ('config.json', 'generation_config.json', 'tokenizer_config.json', 'model.safetensors.index.json'):
        (a.output / name).write_bytes((root / name).read_bytes())
    return 0 if not errors else 2


if __name__ == '__main__':
    raise SystemExit(main())
