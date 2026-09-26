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

SYSTEM_PROMPT = """You convert public meeting agendas from a city government into accessible text for residents.

Rules:
- Use only what the document says. Do not add background, predictions, opinions, or likely outcomes.
- Neutral tone. No adjectives that judge (such as important, controversial, significant).
- Plain English at about an 8th-grade reading level. Spell out acronyms the first time if the document defines them; otherwise keep them as written.
- Keep names, addresses, dates, times, dollar amounts, and case or application numbers exactly as written.
- If part of the document cannot be read, say "[unreadable]" in the transcript rather than guessing."""

USER_PROMPT = """This is the posted agenda for: {title}, {date}.

Return:
- transcript: the full text of the agenda in reading order, as Markdown. Use a heading for the meeting name, and a numbered or bulleted list for agenda items as they appear. Leave out stamps, seals, and page decorations, but keep the clerk's posting date if shown.
- summary: 1 to 3 sentences on what the meeting will cover. Name the main business items.
- items: each agenda item, in order, as short plain-English phrases. Skip routine items such as call to order, roll call, and adjournment.
- attend: how the public can attend or comment, as stated in the document (place, remote link, phone), in 1 to 2 sentences. Empty string if the document does not say."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string"},
        "summary": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
        "attend": {"type": "string"},
    },
    "required": ["transcript", "summary", "items", "attend"],
    "additionalProperties": False,
}


def summaries_dir(data_dir: Path) -> Path:
    return data_dir / "summaries"


def cached(data_dir: Path, sha256: str) -> dict | None:
    path = summaries_dir(data_dir) / f"{sha256}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def pending_agendas(data_dir: Path, today: str) -> list[tuple[dict, dict]]:
    """Latest agenda of each meeting without a cached summary, upcoming meetings first."""
    path = data_dir / "meetings" / "meetings.json"
    store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo, seen = [], set()
    for meeting in store.values():
        if not meeting.get("agendas"):
            continue
        agenda = meeting["agendas"][-1]
        # Several meetings can share one document; process it once.
        if agenda["sha256"] in seen or cached(data_dir, agenda["sha256"]):
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
    todo = pending_agendas(data_dir, now.date().isoformat())
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
