"""Where saved agenda and minutes PDFs are kept.

By default they are committed with the rest of the data, under
data/meetings/agendas/ and data/meetings/minutes/, and copied into the built
site. That doesn't last: the PDFs average well over a megabyte, git keeps every
one forever, and a GitHub Pages site may be at most 1 GB. A town with a
[storage] table keeps them in an S3-compatible bucket (Cloudflare R2) instead,
served from the bucket's public address. Git then keeps only what is read from
each PDF (its text, summary and SHA-256 hash).

  [storage]
  endpoint = "https://<account id>.r2.cloudflarestorage.com"
  bucket = "publick-documents"
  public_url = "https://files.publick.org"
  prefix = "gloucester"   # optional; defaults to the town's config name

The keys come from the STORAGE_ACCESS_KEY_ID and STORAGE_SECRET_ACCESS_KEY
environment variables. Building the site needs no keys, only public_url.
Setting DOCUMENTS_LOCAL=1 keeps PDFs under data/meetings even for a town with
a bucket: the tests do this, and so can a local run without keys.

A PDF still in data/meetings (saved before the town had a bucket) is read and
linked from there, so a site builds correctly before, during and after the
move. To move a town's saved PDFs into its bucket:

    python -m pipeline.documents upload [--town gloucester]

It uploads each one, checks the bucket copy, and deletes the local file; the
daily workflow runs it and commits the deletions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pipeline.config import DATA_DIR, DEFAULT_TOWN, load_config
from pipeline.http import FetchError

FOLDERS = ("agendas", "minutes")
# A saved file never changes: a changed agenda is a new document with a new id.
CACHE_CONTROL = "public, max-age=31536000, immutable"


class LocalDocuments:
    """PDFs committed under data/meetings/<folder>/ and copied into the site."""

    def __init__(self, data_dir: Path):
        self.root = data_dir / "meetings"

    def local_path(self, folder: str, name: str) -> Path:
        return self.root / folder / name

    def put(self, folder: str, name: str, content: bytes) -> None:
        path = self.local_path(folder, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, folder: str, name: str) -> bytes:
        return self.local_path(folder, name).read_bytes()

    def url(self, folder: str, name: str) -> str:
        return f"/meetings/{folder}/{name}"


class BucketDocuments(LocalDocuments):
    """PDFs in an S3-compatible bucket, under <prefix>/<folder>/<name>."""

    def __init__(self, settings: dict, prefix: str, data_dir: Path):
        super().__init__(data_dir)
        self.settings = settings
        self.bucket = settings["bucket"]
        self.prefix = settings.get("prefix", prefix).strip("/")
        self.public_url = settings["public_url"].rstrip("/")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = make_client(self.settings)
        return self._client

    def key(self, folder: str, name: str) -> str:
        return f"{self.prefix}/{folder}/{name}"

    def put(self, folder: str, name: str, content: bytes) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.key(folder, name), Body=content,
                                   ContentType="application/pdf", CacheControl=CACHE_CONTROL)
        except Exception as e:  # botocore errors; the caller moves on to the next document
            raise FetchError(f"storage: could not save {folder}/{name}: {e}") from e

    def get(self, folder: str, name: str) -> bytes:
        local = self.local_path(folder, name)
        if local.exists():
            return local.read_bytes()
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self.key(folder, name))["Body"].read()
        except Exception as e:
            raise FetchError(f"storage: could not read {folder}/{name}: {e}") from e

    def url(self, folder: str, name: str) -> str:
        if self.local_path(folder, name).exists():
            return super().url(folder, name)
        return f"{self.public_url}/{self.key(folder, name)}"

    def stored_size(self, folder: str, name: str) -> int | None:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=self.key(folder, name))["ContentLength"]
        except Exception:
            return None


def make_client(settings: dict):
    key_id, secret = os.environ.get("STORAGE_ACCESS_KEY_ID"), os.environ.get("STORAGE_SECRET_ACCESS_KEY")
    if not key_id or not secret:
        raise FetchError("storage: STORAGE_ACCESS_KEY_ID and STORAGE_SECRET_ACCESS_KEY must be set")
    import boto3

    return boto3.client("s3", endpoint_url=settings["endpoint"], aws_access_key_id=key_id,
                        aws_secret_access_key=secret, region_name=settings.get("region", "auto"))


def open_documents(config: dict, data_dir: Path) -> LocalDocuments:
    """The town's document store: its bucket if it has a [storage] table, else data/meetings."""
    if "storage" in config and not os.environ.get("DOCUMENTS_LOCAL"):
        return BucketDocuments(config["storage"], config["slug"], data_dir)
    return LocalDocuments(data_dir)


def upload(store: BucketDocuments) -> dict:
    """Move every PDF still under data/meetings into the bucket. A local file is
    deleted only once the bucket holds a copy of the same size."""
    moved, errors = 0, []
    for folder in FOLDERS:
        directory = store.root / folder
        for path in sorted(directory.glob("*.pdf")) if directory.exists() else []:
            content = path.read_bytes()
            try:
                if store.stored_size(folder, path.name) != len(content):
                    store.put(folder, path.name, content)
                if store.stored_size(folder, path.name) != len(content):
                    raise FetchError(f"storage: {folder}/{path.name} did not arrive intact")
            except FetchError as e:
                errors.append(str(e))
                continue
            path.unlink()
            moved += 1
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
    return {"moved": moved, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["upload"])
    parser.add_argument("--town", default=DEFAULT_TOWN)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    config = load_config(args.town)
    store = open_documents(config, args.data)
    if not isinstance(store, BucketDocuments):
        print(f"::notice::No [storage] in config/{args.town}.toml; PDFs stay in data/meetings.")
        return 0
    try:
        result = upload(store)
    except FetchError as e:
        print(f"::error::{e}")
        return 1
    print(json.dumps(result, indent=2))
    for error in result["errors"]:
        print(f"::warning::{error}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
