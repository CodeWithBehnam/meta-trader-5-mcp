"""Run an order intent: guard, order_check, audited intent, order_send, outcome."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import audit, shaping
from .bridge_client import BridgeConnectionLost, BridgeError, BridgeUnavailable
from .context import AppContext
from .enums import is_success_retcode
from .guards import CancelOrder, GuardRefusal, Intent, Rejected, check_intent
from .launcher import LauncherError
from .orders import build_request

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 60.0


def _write_audit(
    ctx: AppContext,
    tool: str,
    request: dict[str, Any],
    check: dict[str, Any] | None,
    send: dict[str, Any] | None,
    outcome: str,
) -> str | None:
    record = audit.make_record(
        timestamp=ctx.audit_time(),
        tool=tool,
        request=request,
        check=check,
        send=send,
        outcome=outcome,
    )
    try:
        audit.append_record(ctx.config.audit_log, record)
    except OSError as exc:
        logger.error("Could not write audit log %s: %s", ctx.config.audit_log, exc)
        return f"audit log write failed: {exc}"
    return None


def dry_run(ctx: AppContext, intent: Intent) -> dict[str, Any]:
    """Validate and run order_check only; nothing is sent."""
    rejection = check_intent(intent, ctx.config, require_trading=False)
    if isinstance(rejection, Rejected):
        raise ToolError(rejection.reason)
    request = build_request(intent)
    check = ctx.bridge.call("order_check", {"request": request})
    summary = shaping.summarise_check(check)
    return {"ok": check.get("retcode") == 0, "stage": "check", "request": request, **summary}


def run_intent(ctx: AppContext, tool: str, resolve: Callable[[], Intent]) -> dict[str, Any]:
    """Resolve an intent, then execute it; guard refusals raised while resolving are audited."""
    try:
        intent = resolve()
    except GuardRefusal as exc:
        _write_audit(ctx, tool, exc.request, None, None, f"guard_rejected: {exc.reason}")
        raise
    return execute(ctx, tool, intent)


def execute(ctx: AppContext, tool: str, intent: Intent) -> dict[str, Any]:
    rejection = check_intent(intent, ctx.config)
    request = build_request(intent)
    if isinstance(rejection, Rejected):
        _write_audit(ctx, tool, request, None, None, f"guard_rejected: {rejection.reason}")
        raise ToolError(f"Refused by guard: {rejection.reason}")

    check_summary: dict[str, Any] | None = None
    # MT5 documents order_check for requests that open or change trades; a plain REMOVE has
    # nothing to validate beyond the ticket already looked up, so it goes straight to send.
    if not isinstance(intent, CancelOrder):
        try:
            check = ctx.bridge.call("order_check", {"request": request})
        except (BridgeError, LauncherError) as exc:
            _write_audit(ctx, tool, request, {"error": str(exc)}, None, "check_error")
            raise
        check_summary = shaping.summarise_check(check)
        if check.get("retcode") != 0:
            audit_error = _write_audit(ctx, tool, request, check_summary, None, "check_rejected")
            return _result(False, "check", check_summary, None, check_summary, audit_error)

    # Fail closed: the intent is on disk before any money can move. If it cannot be written
    # the order is not sent.
    intent_error = _write_audit(ctx, tool, request, check_summary, None, "intent")
    if intent_error is not None:
        raise ToolError(
            f"Order not sent: {intent_error}. Fix MT5_AUDIT_LOG ({ctx.config.audit_log}) so "
            "every trade can be recorded, then retry."
        )

    try:
        send = ctx.bridge.call("order_send", {"request": request}, timeout=SEND_TIMEOUT_SECONDS)
    except BridgeConnectionLost as exc:
        _write_audit(ctx, tool, request, check_summary, {"error": str(exc)}, "send_unknown")
        raise ToolError(
            f"Connection to the bridge failed during order_send ({exc}). The order may or may "
            "not have been executed; check get_positions, get_orders and get_history_deals "
            "before retrying."
        ) from exc
    except BridgeUnavailable as exc:
        _write_audit(ctx, tool, request, check_summary, {"error": str(exc)}, "send_not_delivered")
        raise ToolError(
            f"The bridge could not be reached for order_send ({exc}). The order was not sent."
        ) from exc
    except (BridgeError, LauncherError) as exc:
        _write_audit(ctx, tool, request, check_summary, {"error": str(exc)}, "send_error")
        raise

    send_summary = shaping.summarise_send(send)
    ok = is_success_retcode(send.get("retcode"))
    audit_error = _write_audit(
        ctx, tool, request, check_summary, send_summary, "sent" if ok else "send_rejected"
    )
    return _result(ok, "send", send_summary, send_summary, check_summary, audit_error)


def _result(
    ok: bool,
    stage: str,
    headline: dict[str, Any],
    send: dict[str, Any] | None,
    check: dict[str, Any] | None,
    audit_error: str | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "stage": stage, **headline}
    if send is not None and check is not None:
        out["check"] = check
    if audit_error is not None:
        out["audit_error"] = audit_error
    return out
