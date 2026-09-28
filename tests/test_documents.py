"""Agenda and minutes PDFs kept in a bucket instead of git (pipeline/documents.py)."""

import copy
import io

import pytest
from conftest import BUILT_AT, FETCHED_AT
from fakes import FakeAnthropic, FakeCityClient

from pipeline import build_site, documents, fetch_meetings, fetch_minutes, summarize
from pipeline.config import load_config
from pipeline.http import FetchError

PUBLIC_URL = "https://files.publick.example"


class FakeBucket:
    """The few S3 calls the store makes, kept in a dict."""

    def __init__(self, fail_puts: bool = False):
        self.objects = {}
        self.fail_puts = fail_puts

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        if self.fail_puts:
            raise RuntimeError("bucket unavailable")
        self.objects[Key] = {"body": bytes(Body), "type": ContentType, "cache": CacheControl}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key]["body"])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("404")
        return {"ContentLength": len(self.objects[Key]["body"])}


def with_storage(config: dict) -> dict:
    town = copy.deepcopy(config)
    town["storage"] = {"endpoint": "https://account.r2.example", "bucket": "documents", "public_url": PUBLIC_URL + "/"}
    return town


@pytest.fixture(autouse=True)
def use_buckets(monkeypatch):
    monkeypatch.delenv("DOCUMENTS_LOCAL", raising=False)


@pytest.fixture
def bucket(monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setattr(documents, "make_client", lambda settings: bucket)
    return bucket


def without_storage(config: dict) -> dict:
    town = copy.deepcopy(config)
    town.pop("storage", None)
    return town


def test_town_without_storage_keeps_pdfs_in_data(tmp_path):
    store = documents.open_documents(without_storage(load_config("gloucester")), tmp_path)
    assert type(store) is documents.LocalDocuments
    store.put("agendas", "1.pdf", b"%PDF-1")
    assert (tmp_path / "meetings" / "agendas" / "1.pdf").read_bytes() == b"%PDF-1"
    assert store.url("agendas", "1.pdf") == "/meetings/agendas/1.pdf"


def test_fetched_pdfs_go_to_the_bucket_not_git(tmp_path, bucket):
    config = with_storage(load_config("gloucester"))
    fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    fetch_minutes.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    assert not list((tmp_path / "meetings").rglob("*.pdf"))
    assert "gloucester/agendas/20124.pdf" in bucket.objects
    assert any(k.startswith("gloucester/minutes/") for k in bucket.objects)
    saved = bucket.objects["gloucester/agendas/20124.pdf"]
    assert saved["body"].startswith(b"%PDF") and saved["type"] == "application/pdf"
    assert "immutable" in saved["cache"]


def test_summaries_read_pdfs_from_the_bucket(tmp_path, bucket):
    config = with_storage(load_config("gloucester"))
    fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    client = FakeAnthropic()
    assert summarize.run(config, client, tmp_path, limit=50, now=FETCHED_AT)["summarized"] == 1
    assert client.calls[0]["messages"][0]["content"][0]["type"] == "document"


def test_bucket_failure_leaves_the_agenda_to_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "make_client", lambda settings: FakeBucket(fail_puts=True))
    config = with_storage(load_config("gloucester"))
    result = fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    assert any("storage" in e for e in result["errors"])
    store = fetch_meetings.load_store(tmp_path)
    assert not any(m.get("agendas") for m in store.values())


def test_missing_keys_are_reported_per_document(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_ACCESS_KEY_ID", raising=False)
    store = documents.open_documents(with_storage(load_config("gloucester")), tmp_path)
    with pytest.raises(FetchError, match="STORAGE_ACCESS_KEY_ID"):
        store.put("agendas", "1.pdf", b"%PDF-1")


def test_documents_local_keeps_pdfs_in_data_despite_a_bucket(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENTS_LOCAL", "1")
    assert type(documents.open_documents(with_storage(load_config("gloucester")), tmp_path)) is documents.LocalDocuments


def test_upload_moves_local_pdfs_and_keeps_links_working(tmp_path, bucket):
    config = without_storage(load_config("gloucester"))
    fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    fetch_minutes.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    local = sorted(p.relative_to(tmp_path / "meetings") for p in (tmp_path / "meetings").rglob("*.pdf"))
    assert local

    store = documents.open_documents(with_storage(config), tmp_path)
    # Before the move, links point at the copies still in the repository.
    assert store.url("agendas", "20124.pdf") == "/meetings/agendas/20124.pdf"

    assert documents.upload(store) == {"moved": len(local), "errors": []}
    assert not (tmp_path / "meetings" / "agendas").exists()
    assert sorted(bucket.objects) == sorted(f"gloucester/{p}" for p in local)
    assert store.url("agendas", "20124.pdf") == f"{PUBLIC_URL}/gloucester/agendas/20124.pdf"
    assert store.get("agendas", "20124.pdf").startswith(b"%PDF")
    # Running again finds nothing left to move.
    assert documents.upload(store) == {"moved": 0, "errors": []}


def test_upload_keeps_a_file_the_bucket_did_not_take(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "make_client", lambda settings: FakeBucket(fail_puts=True))
    store = documents.open_documents(with_storage(load_config("gloucester")), tmp_path)
    (tmp_path / "meetings" / "minutes").mkdir(parents=True)
    (tmp_path / "meetings" / "minutes" / "7.pdf").write_bytes(b"%PDF-7")
    result = documents.upload(store)
    assert result["moved"] == 0 and result["errors"]
    assert (tmp_path / "meetings" / "minutes" / "7.pdf").exists()


def test_site_links_to_bucket_copies(tmp_path, bucket, monkeypatch):
    config = with_storage(load_config("gloucester"))
    data = tmp_path / "data"
    fetch_meetings.run(config, FakeCityClient(), data, now=FETCHED_AT)
    fetch_minutes.run(config, FakeCityClient(), data, now=FETCHED_AT)
    monkeypatch.setattr(build_site, "load_config", lambda slug: config)
    out = tmp_path / "site"
    build_site.build("gloucester", out, data_dir=data, now=BUILT_AT)
    assert not list(out.rglob("*.pdf"))
    pages = "".join(p.read_text() for p in (out / "meetings").rglob("index.html"))
    assert f'href="{PUBLIC_URL}/gloucester/agendas/20124.pdf"' in pages
    assert f'href="{PUBLIC_URL}/gloucester/minutes/' in pages
    assert 'href="/meetings/agendas/' not in pages and 'href="/meetings/minutes/' not in pages
