"""Turn posted meeting agendas into readable text and plain-English previews.

The city posts agendas as scanned images, which screen readers cannot read.
For each saved agenda PDF, a language model transcribes the full text and
writes a short neutral preview. Results are cached by the PDF's SHA-256 hash
in data/summaries/, so an unchanged document is never processed twice.

Needs ANTHROPIC_API_KEY. Without it, the step is skipped.

Usage:
    python -m pipeline.summarize [--town gloucester] [--limit 20]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.config import DATA_DIR, load_config

# Bump when the prompt or schema changes; cached results from an older
# version (or another model) are regenerated on the next run.
PROMPT_VERSION = 2

SYSTEM_PROMPT = """You convert public meeting agendas from a city government into accessible text for residents. The agendas are usually scanned images, so read every character carefully.

Transcript rules:
- Copy the document's text character for character. Do not reword, correct, modernize, or change the spelling of anything (for example, keep "Councilor" if that is how it is written).
- Take particular care with digits and similar-looking characters (0 and O, 1 and l and I, 5 and S) in ZIP codes, phone numbers, meeting IDs, web addresses, dollar amounts, dates, and case numbers.
- If a word or number cannot be read with confidence, write "[unreadable]" instead of guessing.

Summary and item rules:
- Use only what the document says. Do not add background, predictions, opinions, likely outcomes, or categories the document does not use.
- Neutral tone. No adjectives that judge (such as important, controversial, significant).
- Plain English at about an 8th-grade reading level.
- Keep names, dollar amounts, dates, and case or application numbers exactly as written."""

USER_PROMPT = """This is the posted agenda for: {title}, {date}.

Return:
- transcript: the full text of the agenda in reading order, as Markdown. Use headings for the document's own headings and lists for its lists. Leave out stamps, seals, and page decorations, but keep the clerk's posting date if shown.
- summary: 1 to 3 sentences on what the meeting will cover. Name the main business items.
- items: each agenda item, in order, as short plain-English phrases. Skip routine items such as call to order, roll call, approval of minutes, and adjournment."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string"},
        "summary": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["transcript", "summary", "items"],
    "additionalProperties": False,
}


def summaries_dir(data_dir: Path) -> Path:
    return data_dir / "summaries"


def cached(data_dir: Path, sha256: str, model: str) -> dict | None:
    """The saved result for a document, if it was made by this model and prompt version."""
    path = summaries_dir(data_dir) / f"{sha256}.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("model") != model or record.get("prompt_version") != PROMPT_VERSION:
        return None
    return record


def pending_agendas(data_dir: Path, today: str, model: str) -> list[tuple[dict, dict]]:
    """Latest agenda of each meeting without a cached summary, upcoming meetings first."""
    path = data_dir / "meetings" / "meetings.json"
    store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo, seen = [], set()
    for meeting in store.values():
        if not meeting.get("agendas"):
            continue
        agenda = meeting["agendas"][-1]
        # Several meetings can share one document; process it once.
        if agenda["sha256"] in seen or cached(data_dir, agenda["sha256"], model):
            continue
        seen.add(agenda["sha256"])
        todo.append((meeting, agenda))
    upcoming = sorted((ma for ma in todo if ma[0]["date"] >= today), key=lambda ma: ma[0]["date"])
    past = sorted((ma for ma in todo if ma[0]["date"] < today), key=lambda ma: ma[0]["date"], reverse=True)
    return upcoming + past


def summarize_pdf(client, model: str, pdf: bytes, title: str, date: str, max_tokens: int) -> tuple[dict, dict]:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(pdf).decode("ascii")}},
                {"type": "text", "text": USER_PROMPT.format(title=title, date=date)},
            ],
        }],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"stopped early: {response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    return json.loads(text), usage


def run(config: dict, client, data_dir: Path, limit: int, now: datetime | None = None) -> dict:
    settings = config["summaries"]
    now = now or datetime.now(ZoneInfo(config["site"]["timezone"]))
    todo = pending_agendas(data_dir, now.date().isoformat(), settings["model"])
    done, errors, tokens = 0, [], {"input_tokens": 0, "output_tokens": 0}
    for meeting, agenda in todo[:limit]:
        pdf = (data_dir / "meetings" / "agendas" / agenda["file"]).read_bytes()
        try:
            result, usage = summarize_pdf(client, settings["model"], pdf, meeting["title"], meeting["date"], settings["max_tokens"])
        except Exception as e:  # one bad document must not stop the rest
            errors.append(f"agenda {agenda['id']}: {e}")
            continue
        record = {
            **result,
            "kind": "agenda",
            "source_url": agenda["source_url"],
            "source_sha256": agenda["sha256"],
            "model": settings["model"],
            "prompt_version": PROMPT_VERSION,
            "generated_at": now.isoformat(timespec="seconds"),
            "usage": usage,
        }
        path = summaries_dir(data_dir) / f"{agenda['sha256']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for k in tokens:
            tokens[k] += usage[k]
        done += 1
    return {"summarized": done, "remaining": max(len(todo) - done, 0), "errors": errors, **tokens}


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
