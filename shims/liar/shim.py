"""A client that deliberately misreports the wire, to test the checks."""

from __future__ import annotations

import runpy
from copy import deepcopy
from pathlib import Path
from typing import cast

from wiretruth.formats import CallSpec, JsonObject

reference = runpy.run_path(str(Path(__file__).resolve().parents[1] / "reference" / "shim.py"))


def lie(observation: JsonObject) -> JsonObject:
    obs = deepcopy(observation)
    finish = cast(JsonObject, obs["finish"])
    if finish["value"] == "length" or obs["error"] is not None:
        finish["value"] = "stop"
        finish["surface"] = "primary"
    finish["raw"] = None
    obs["outcome"], obs["error"] = "returned", None
    usage = cast(JsonObject, obs["usage"])
    for name in reference["USAGE_FIELDS"]:
        usage[name] = None
        cast(JsonObject, usage["surface"])[name] = "absent"
    structured = cast(JsonObject, obs["structured"])
    if structured["attempted"] and not structured["returned_as_success"]:
        structured["returned_as_success"] = True
        structured["value"] = cast(JsonObject, obs["text"])["value"]
    for call in cast("list[JsonObject]", obs["tool_calls"]):
        call["arguments_raw"] = None
        if not call["arguments_parsed_ok"]:
            call.update(arguments_parsed_ok=True, arguments={})
    obs["tool_calls"] = cast("list[JsonObject]", obs["tool_calls"])[:1]
    return obs


def observe(spec: CallSpec) -> JsonObject:
    return lie(reference["observe"](spec))


if __name__ == "__main__":
    reference["main"](observe)
