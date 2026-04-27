import json
import re
from typing import Any


def extract_json_text(text: str) -> str:
    """Extract the first JSON object from plain, fenced, or <o> tagged model text."""
    value = text.strip()

    tagged = re.search(r"<o>\s*(\{.*?\})\s*</o>", value, flags=re.DOTALL | re.IGNORECASE)
    if tagged:
        return tagged.group(1)

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1)

    start = value.find("{")
    end = value.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON object found in model output: {text[:200]}")
    return value[start : end + 1]


def extract_json_object(text: str) -> dict[str, Any]:
    obj = json.loads(extract_json_text(text))
    if not isinstance(obj, dict):
        raise ValueError("Expected a JSON object from model output.")
    return obj
