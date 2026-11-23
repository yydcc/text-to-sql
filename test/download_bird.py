"""Fetch BIRD Mini-Dev PostgreSQL annotations and official SQL dump with HTTP ranges."""
from __future__ import annotations

import hashlib
import json
import urllib.request
import zipfile
from pathlib import Path

HF_REV = '5b256b5a9c17478f06ed6912612e9a14d17065c8'
HF_URL = f'https://huggingface.co/datasets/birdsql/bird_mini_dev/resolve/{HF_REV}/data/mini_dev_postgresql.jsonl'
ZIP_URL = 'https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip'
DUMP_NAME = 'minidev/MINIDEV_postgresql/BIRD_dev.sql'


class RemoteZip:
    def __init__(self, url):
        self.url = url
        request = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(request, timeout=30) as response:
            self.size = int(response.headers['Content-Length'])
        self.position = 0

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = (offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset)
        return self.position

    def read(self, size=-1):
        if size < 0:
            size = self.size - self.position
        if size == 0:
            return b''
        request = urllib.request.Request(self.url, headers={'Range': f'bytes={self.position}-{self.position+size-1}'})
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 206:
                raise RuntimeError('Server did not honor HTTP Range')
            data = response.read()
        self.position += len(data)
        return data


def main():
    root = Path('test/assets')
    root.mkdir(parents=True, exist_ok=True)
    annotations = root / 'mini_dev_postgresql.jsonl'
    if not annotations.exists():
        with urllib.request.urlopen(HF_URL, timeout=120) as response:
            annotations.write_bytes(response.read())
    rows = [json.loads(line) for line in annotations.read_text().splitlines()]
    if len(rows) != 500 or {row['question_id'] for row in rows} != set(range(500)):
        raise RuntimeError('Expected 500 PostgreSQL Mini-Dev rows with IDs 0–499')
    dump = root / 'BIRD_dev.sql'
    if not dump.exists():
        partial = dump.with_suffix('.sql.partial')
        digest = hashlib.sha256()
        with zipfile.ZipFile(RemoteZip(ZIP_URL)) as archive, archive.open(DUMP_NAME) as source, partial.open('wb') as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
        partial.replace(dump)
        print('dump_sha256', digest.hexdigest())
    print('annotations', annotations, len(rows), 'dump', dump, dump.stat().st_size)


if __name__ == '__main__':
    main()
