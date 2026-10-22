"""Telegram command menu, shared with the authenticated command dispatcher."""

COMMAND_DESCRIPTIONS = {
    "/help": "Show all commands and how to use them",
    "/status": "Current bot state and open positions",
    "/strategy": "Current strategy, confidence and switch history",
    "/summary": "Previous 24h: wins, losses, PnL, errors and locks",
    "/why": "Explain current entry blockers and the 12h/2h market trend",
    "/positions": "Show open positions",
    "/orders": "Show recent order states",
    "/risk": "Show risk limits, current locks and account diagnostics",
    "/last_signal": "Show the latest trading signal and its reason",
    "/daily": "Show daily report and account diagnostics",
    "/health": "Show bot, exchange and AI diagnostics",
    "/version": "Show strategy and configuration details",
    "/pause": "Pause new entries; continue managing open positions",
    "/stop": "Pause new entries; continue managing open positions",
    "/lock": "Lock new entries; continue managing open positions",
    "/resume": "Resume after checks and reset financial locks",
    "/start_trading": "Resume after checks and reset financial locks",
    "/refuse_force": "Reset locks and bypass resume-time scan and crisis checks",
    "/flatten": "Request closing all positions; confirmation code required",
    "/confirm_flatten": "Confirm closure using the code returned by /flatten",
}
COMMANDS = set(COMMAND_DESCRIPTIONS)


def menu_commands() -> list[dict[str, str]]:
    return [
        {"command": name[1:], "description": description}
        for name, description in COMMAND_DESCRIPTIONS.items()
    ]
