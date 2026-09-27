"""Turn posted agendas and minutes into readable text and plain-English summaries.

The city posts these as scanned images, which screen readers cannot read.
For each saved PDF, a language model transcribes the full text and writes a
short neutral summary: a preview for an agenda, a record of decisions for
minutes. Results are cached by the PDF's SHA-256 hash in data/summaries/, so
an unchanged document is never processed twice.

Needs ANTHROPIC_API_KEY. Without it, the step is skipped.

Usage:
    python -m pipeline.summarize [--town gloucester] [--limit 20]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from pipeline.config import DATA_DIR, load_config

TRANSCRIPT_RULES = """Transcript rules:
- Copy the document's text character for character. Do not reword, correct, modernize, or change the spelling of anything (for example, keep "Councilor" if that is how it is written).
- Take particular care with digits and similar-looking characters (0 and O, 1 and l and I, 5 and S) in ZIP codes, phone numbers, meeting IDs, web addresses, dollar amounts, dates, vote counts, and case numbers.
- If a word or number cannot be read with confidence, write "[unreadable]" instead of guessing."""

SUMMARY_RULES = """Summary rules:
- Use only what the document says. Do not add background, predictions, opinions, likely outcomes, or categories the document does not use.
- Neutral tone. No adjectives that judge (such as important, controversial, significant).
- Plain English at about an 8th-grade reading level. Short sentences, one idea each.
- Keep names, dollar amounts, dates, and case or application numbers exactly as written."""

# Each kind of document has its own instructions and output. Bump a kind's
# version when its prompt or schema changes; cached results from an older
# version (or another model) are regenerated on the next run.
KINDS = {
    "agenda": {
        "version": 3,
        "folder": "agendas",
        "max_tokens": 16000,
        "system": "You convert public meeting agendas from a city government into accessible text for residents. The agendas are usually scanned images, so read every character carefully.\n\n"
                  + TRANSCRIPT_RULES + "\n\n" + SUMMARY_RULES,
        "prompt": """This is the posted agenda for: {title}, {date}.

Return:
- transcript: the full text of the agenda in reading order, as Markdown. Use headings for the document's own headings and lists for its lists. Leave out stamps, seals, and page decorations, but keep the clerk's posting date if shown.
- headline: one sentence of at most 25 words saying what the meeting will take up, naming the main business. Do not mention the board's name, the date, the time, or the place; readers already see those.
- summary: 1 or 2 short sentences on what the meeting will cover. Name the main business items. Do not repeat the board's name, the date, the time, or the place.
- items: each agenda item, in order, as short plain-English phrases. Skip routine items such as call to order, roll call, approval of minutes, and adjournment.""",
        "schema": {
            "type": "object",
            "properties": {
                "transcript": {"type": "string"},
                "headline": {"type": "string"},
                "summary": {"type": "string"},
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["transcript", "headline", "summary", "items"],
            "additionalProperties": False,
        },
    },
    "minutes": {
        "version": 1,
        "folder": "minutes",
        "max_tokens": 64000,
        "system": "You convert the minutes of public meetings of a city government into accessible text for residents. Minutes are usually scanned images, so read every character carefully.\n\n"
                  + TRANSCRIPT_RULES + "\n\n" + SUMMARY_RULES + """
- Report decisions only as the minutes record them. Include the vote count or roll call result when the minutes give one. If the minutes do not say how a matter ended, do not list it as a decision.
- Use the minutes' own verb for each outcome (approved, recommended, referred, continued, tabled, denied). A vote to recommend is not an approval.""",
        "prompt": """These are the posted minutes for: {title}, {date}.

Return:
- transcript: the full text of the minutes in reading order, as Markdown. Use headings for the document's own headings and lists for its lists. Leave out stamps, seals, and page decorations.
- headline: one sentence of at most 25 words on what the meeting decided, or what it discussed if it decided nothing. Do not mention the board's name, the date, the time, or the place; readers already see those.
- summary: 1 to 3 short sentences on what the meeting covered and what was decided. Do not repeat the board's name, the date, the time, or the place.
- is_minutes: true if this document is minutes of a meeting that took place; false if it is something else, such as an agenda or notice filed under minutes.
- decisions: each motion, vote, or other decision the minutes record, in order, as a short plain-English sentence that includes the outcome (for example "Approved ... 5-0" or "Continued ... to October 22, 2026"). Skip procedural motions such as adjourning or accepting the agenda.""",
        "schema": {
            "type": "object",
            "properties": {
                "transcript": {"type": "string"},
                "headline": {"type": "string"},
                "summary": {"type": "string"},
                "is_minutes": {"type": "boolean"},
                "decisions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["transcript", "headline", "summary", "is_minutes", "decisions"],
            "additionalProperties": False,
        },
    },
}

# Documents longer or larger than this are not sent; the page links to the original.
MAX_PAGES = 60
# The API takes requests up to 32 MB, and base64 makes a PDF a third larger.
MAX_BYTES = 22_000_000


def too_large(doc: dict) -> bool:
    return (doc.get("bytes") or 0) > MAX_BYTES


def summaries_dir(data_dir: Path) -> Path:
    return data_dir / "summaries"


def cached(data_dir: Path, sha256: str, model: str, kind: str = "agenda") -> dict | None:
    """The saved result for a document, if it was made by this model and the current prompt."""
    path = summaries_dir(data_dir) / f"{sha256}.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    version = KINDS[kind]["version"]
    if record.get("model") != model or record.get("prompt_version") != version or record.get("kind", "agenda") != kind:
        return None
    return record


def pending_documents(data_dir: Path, today: str, model: str) -> list[tuple[str, dict, dict]]:
    """Latest agenda and minutes of each meeting without a current summary.

    Order: agendas for upcoming meetings, then minutes (newest meeting first),
    then agendas for past meetings."""
    path = data_dir / "meetings" / "meetings.json"
    store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo, seen = [], set()
    for meeting in store.values():
        for kind, field in (("agenda", "agendas"), ("minutes", "minutes")):
            if not meeting.get(field):
                continue
            doc = meeting[field][-1]
            # Several meetings can share one document; process it once.
            if doc["sha256"] in seen or too_large(doc) or cached(data_dir, doc["sha256"], model, kind):
                continue
            seen.add(doc["sha256"])
            todo.append((kind, meeting, doc))
    date = lambda t: t[1]["date"]
    upcoming = sorted((t for t in todo if t[0] == "agenda" and date(t) >= today), key=date)
    minutes = sorted((t for t in todo if t[0] == "minutes"), key=date, reverse=True)
    past = sorted((t for t in todo if t[0] == "agenda" and date(t) < today), key=date, reverse=True)
    return upcoming + minutes + past


def page_count(pdf: bytes) -> int | None:
    try:
        return len(PdfReader(io.BytesIO(pdf)).pages)
    except Exception:
        return None


def summarize_pdf(client, model: str, kind: str, pdf: bytes, title: str, date: str) -> tuple[dict, dict]:
    spec = KINDS[kind]
    # Streaming avoids HTTP timeouts on long transcripts.
    with client.messages.stream(
        model=model,
        max_tokens=spec["max_tokens"],
        system=spec["system"],
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(pdf).decode("ascii")}},
                {"type": "text", "text": spec["prompt"].format(title=title, date=date)},
            ],
        }],
        output_config={"format": {"type": "json_schema", "schema": spec["schema"]}},
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"stopped early: {response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    return json.loads(text), usage


def cost(usage: dict, settings: dict) -> float:
    return (usage["input_tokens"] * settings["input_price"] + usage["output_tokens"] * settings["output_price"]) / 1e6


def run(config: dict, client, data_dir: Path, limit: int, now: datetime | None = None) -> dict:
    settings = config["summaries"]
    now = now or datetime.now(ZoneInfo(config["site"]["timezone"]))
    todo = pending_documents(data_dir, now.date().isoformat(), settings["model"])
    done, errors, tokens, spent = 0, [], {"input_tokens": 0, "output_tokens": 0}, 0.0
    for kind, meeting, doc in todo[:limit]:
        if spent >= settings["max_cost_per_run"]:
            errors.append(f"stopped at the ${settings['max_cost_per_run']:.2f} spending limit for one run")
            break
        pdf = (data_dir / "meetings" / KINDS[kind]["folder"] / doc["file"]).read_bytes()
        pages = page_count(pdf)
        if pages and pages > MAX_PAGES:
            errors.append(f"{kind} {doc['id']}: {pages} pages, over the {MAX_PAGES}-page limit")
            continue
        try:
            result, usage = summarize_pdf(client, settings["model"], kind, pdf, meeting["title"], meeting["date"])
        except Exception as e:  # one bad document must not stop the rest
            if "credit balance" in str(e).lower():
                # Out of API credit: every remaining request would fail the same way.
                errors.append("stopped: the Anthropic account is out of credit; summaries resume when credit is added")
                break
            errors.append(f"{kind} {doc['id']}: {e}")
            continue
        record = {
            **result,
            "kind": kind,
            "source_url": doc["source_url"],
            "source_sha256": doc["sha256"],
            "model": settings["model"],
            "prompt_version": KINDS[kind]["version"],
            "generated_at": now.isoformat(timespec="seconds"),
            "usage": usage,
        }
        path = summaries_dir(data_dir) / f"{doc['sha256']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for k in tokens:
            tokens[k] += usage[k]
        spent += cost(usage, settings)
        done += 1
    return {"summarized": done, "remaining": max(len(todo) - done, 0), "errors": errors,
            "estimated_cost": round(spent, 2), **tokens}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--town", default="gloucester")
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument("--limit", type=int, help="max documents this run")
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("::notice::ANTHROPIC_API_KEY is not set; skipping summaries.")
        return 0
    import anthropic

    config = load_config(args.town)
    client = anthropic.Anthropic(max_retries=3)
    summary = run(config, client, args.data, args.limit or config["summaries"]["max_per_run"])
    print(json.dumps(summary, indent=2))
    for error in summary["errors"]:
        print(f"::warning::{error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
