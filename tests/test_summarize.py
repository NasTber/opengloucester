"""Tests for agenda previews, using a stand-in model client (no API calls)."""

import json

from conftest import FETCHED_AT
from fakes import FakeAnthropic, FakeCityClient
from pipeline import fetch_meetings, summarize
from pipeline.config import load_config


def setup(tmp_path):
    config = load_config("gloucester")
    fetch_meetings.run(config, FakeCityClient(), tmp_path, now=FETCHED_AT)
    return config


def test_preview_is_cached_by_document_hash(tmp_path):
    config = setup(tmp_path)
    client = FakeAnthropic()
    result = summarize.run(config, client, tmp_path, limit=50, now=FETCHED_AT)
    # Every fixture meeting links the same agenda PDF, so it is processed once.
    assert result["summarized"] == 1
    assert len(client.calls) == 1
    again = FakeAnthropic()
    assert summarize.run(config, again, tmp_path, limit=50, now=FETCHED_AT)["summarized"] == 0
    assert again.calls == []


def test_request_sends_pdf_and_asks_for_structured_output(tmp_path):
    config = setup(tmp_path)
    client = FakeAnthropic()
    summarize.run(config, client, tmp_path, limit=1, now=FETCHED_AT)
    call = client.calls[0]
    assert call["model"] == config["summaries"]["model"]
    document = call["messages"][0]["content"][0]
    assert document["type"] == "document" and document["source"]["media_type"] == "application/pdf"
    assert call["output_config"]["format"]["type"] == "json_schema"


def test_saved_preview_records_source_and_model(tmp_path):
    config = setup(tmp_path)
    summarize.run(config, FakeAnthropic(), tmp_path, limit=1, now=FETCHED_AT)
    saved = json.loads(next((tmp_path / "summaries").glob("*.json")).read_text())
    assert saved["source_url"].startswith("https://www.gloucester-ma.gov/Archive.aspx?ADID=")
    assert saved["model"] == config["summaries"]["model"]
    assert saved["summary"] and saved["transcript"] and saved["items"]


def test_truncated_response_is_not_saved(tmp_path):
    config = setup(tmp_path)
    result = summarize.run(config, FakeAnthropic(stop_reason="max_tokens"), tmp_path, limit=1, now=FETCHED_AT)
    assert result["summarized"] == 0 and result["errors"]
    assert not (tmp_path / "summaries").exists()


def test_skips_without_api_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["summarize"])
    assert summarize.main() == 0
    assert "skipping" in capsys.readouterr().out


def test_new_prompt_version_or_model_regenerates(tmp_path, monkeypatch):
    config = setup(tmp_path)
    summarize.run(config, FakeAnthropic(), tmp_path, limit=5, now=FETCHED_AT)
    monkeypatch.setattr(summarize, "PROMPT_VERSION", summarize.PROMPT_VERSION + 1)
    again = FakeAnthropic()
    assert summarize.run(config, again, tmp_path, limit=5, now=FETCHED_AT)["summarized"] == 1
    config["summaries"]["model"] = "another-model"
    assert summarize.run(config, FakeAnthropic(), tmp_path, limit=5, now=FETCHED_AT)["summarized"] == 1
