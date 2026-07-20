"""ADK middleware (callbacks) = cross-cutting concerns WITHOUT extra model round-trips.

  before_tool  - enforce the verify-before-write guardrail + log every call
  after_tool   - turn a tool error into a standard safe-fallback hint + log result

These run in code, deterministically - they are NOT tools, so they add no latency.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("dental.callbacks")

# Tools that touch patient records - blocked until the caller is verified.
GATED_TOOLS = {
    "book_appointment", "reschedule_appointment", "cancel_appointment",
    "list_my_appointments",
}


def before_tool(tool, args, tool_context):
    safe_args = {k: v for k, v in (args or {}).items() if k != "tool_context"}
    logger.info("tool -> %s %s", tool.name, safe_args)
    if tool.name in GATED_TOOLS and not tool_context.state.get("verified"):
        # short-circuit: the model never gets to run the tool
        return {"status": "not_verified",
                "message": "Verify the patient first (last name, date of birth, postcode)."}
    return None


def after_tool(tool, args, tool_context, tool_response):
    status = tool_response.get("status") if isinstance(tool_response, dict) else None
    logger.info("tool <- %s status=%s", tool.name, status)
    if status == "error" and isinstance(tool_response, dict):
        tool_response = dict(tool_response)
        tool_response["fallback"] = (
            "Briefly apologise and offer to take a callback via create_callback_request.")
        tool_context.state["fallback_reason"] = tool.name
    return tool_response
