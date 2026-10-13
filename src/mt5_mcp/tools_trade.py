"""Trading MCP tools, registered only when MT5_ENABLE_TRADING=1."""

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import intents
from .context import AppContext
from .errors import guarded
from .execute import run_intent

Symbol = Annotated[str, Field(description="Broker symbol name, e.g. EURUSD (case-sensitive)")]
Ticket = Annotated[int, Field(gt=0, description="Position or order ticket")]


def register_trading_tools(server: MCPServer, ctx: AppContext) -> None:
    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        )
    )
    @guarded
    def place_order(
        symbol: Symbol,
        side: Literal["BUY", "SELL"],
        volume: Annotated[float, Field(gt=0, description="Lots; capped by MT5_MAX_VOLUME")],
        kind: Annotated[
            Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"],
            Field(description="MARKET executes now; the others create a pending order"),
        ] = "MARKET",
        price: Annotated[float | None, Field(description="Required for pending kinds")] = None,
        stoplimit_price: Annotated[float | None, Field(description="STOP_LIMIT only")] = None,
        sl: Annotated[float | None, Field(description="Stop loss price; omit for none")] = None,
        tp: Annotated[float | None, Field(description="Take profit price; omit for none")] = None,
        deviation: Annotated[
            int | None,
            Field(
                ge=0, description="Max slippage in points (default 20); capped by MT5_MAX_DEVIATION"
            ),
        ] = None,
        magic: Annotated[int, Field(ge=0, description="Expert id to tag the order")] = 0,
        comment: Annotated[str, Field(max_length=31)] = "",
        expiration: Annotated[
            str | None, Field(description="Pending only: ISO-8601 UTC expiry; default GTC")
        ] = None,
        filling: Annotated[
            Literal["FOK", "IOC", "RETURN"] | None,
            Field(description="Default: chosen from the symbol's filling modes"),
        ] = None,
    ) -> dict[str, Any]:
        """Open a position (MARKET at current ask/bid) or place a pending order. Runs guards
        and MT5 order_check first; sends only if the check passes. Real money on live
        accounts. Returns ok, retcode and name, order and deal tickets, fill price."""
        return run_intent(
            ctx,
            "place_order",
            lambda: intents.resolve_place_order(
                ctx,
                symbol=symbol,
                side=side,
                volume=volume,
                kind=kind,
                price=price,
                stoplimit_price=stoplimit_price,
                sl=sl,
                tp=tp,
                deviation=deviation,
                magic=magic,
                comment=comment,
                expiration=expiration,
                filling=filling,
            ),
        )

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    @guarded
    def modify_position_sltp(
        ticket: Ticket,
        sl: Annotated[
            float | None, Field(description="New stop loss price; 0 removes it; omit to keep")
        ] = None,
        tp: Annotated[
            float | None, Field(description="New take profit price; 0 removes it; omit to keep")
        ] = None,
    ) -> dict[str, Any]:
        """Change the stop loss and/or take profit of an open position. Omitted values keep
        the current level; 0 removes the level."""
        return run_intent(
            ctx,
            "modify_position_sltp",
            lambda: intents.resolve_modify_position(ctx, ticket=ticket, sl=sl, tp=tp),
        )

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    @guarded
    def modify_pending_order(
        ticket: Ticket,
        price: Annotated[float | None, Field(description="New entry price; omit to keep")] = None,
        sl: Annotated[float | None, Field(description="0 removes; omit to keep")] = None,
        tp: Annotated[float | None, Field(description="0 removes; omit to keep")] = None,
        stoplimit_price: Annotated[float | None, Field(description="STOP_LIMIT only")] = None,
        expiration: Annotated[
            str | None, Field(description="New ISO-8601 UTC expiry; omit to keep")
        ] = None,
    ) -> dict[str, Any]:
        """Modify a pending order's price, SL/TP, stop-limit price or expiry. Omitted values
        keep their current setting."""
        return run_intent(
            ctx,
            "modify_pending_order",
            lambda: intents.resolve_modify_pending(
                ctx,
                ticket=ticket,
                price=price,
                sl=sl,
                tp=tp,
                stoplimit_price=stoplimit_price,
                expiration=expiration,
            ),
        )

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        )
    )
    @guarded
    def close_position(
        ticket: Ticket,
        volume: Annotated[
            float | None, Field(gt=0, description="Lots to close; default the whole position")
        ] = None,
        deviation: Annotated[
            int | None,
            Field(
                ge=0, description="Max slippage in points (default 20); capped by MT5_MAX_DEVIATION"
            ),
        ] = None,
        comment: Annotated[str, Field(max_length=31)] = "",
    ) -> dict[str, Any]:
        """Close an open position at market (opposite deal), fully or partially. MT5_MAX_VOLUME
        does not limit closing. Realises profit or loss."""
        return run_intent(
            ctx,
            "close_position",
            lambda: intents.resolve_close_position(
                ctx, ticket=ticket, volume=volume, deviation=deviation, comment=comment
            ),
        )

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    @guarded
    def cancel_order(ticket: Ticket) -> dict[str, Any]:
        """Cancel (remove) an active pending order by ticket. Does not touch open positions."""
        return run_intent(
            ctx, "cancel_order", lambda: intents.resolve_cancel_order(ctx, ticket=ticket)
        )
