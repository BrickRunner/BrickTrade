# -*- coding: utf-8 -*-
"""Arbitrage Strategy Settings UI for Telegram.

Provides inline-keyboard-driven configuration for each arbitrage strategy:
  - Enable / disable individual strategies
  - Edit numeric parameters via inline buttons
  - Persist settings to data/arb_settings.json
  - Reset all settings to defaults
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from aiogram import Dispatcher, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
SF = Path("data/arb_settings.json")

# ---------------------------------------------------------------------------
# Strategy meta-definitions
# ---------------------------------------------------------------------------
STRATS: Dict[str, Dict[str, Any]] = {
    "futures_cross_exchange": {
        "icon": "FUT", "name": "Futures Cross-Exchange",
        "desc": "Spot vs futures spread across exchanges",
        "params": {
            "min_spread_pct": ("Min Spread %", 0.15),
            "target_profit_pct": ("Target Profit %", 0.08),
            "max_spread_risk_pct": ("Max Risk %", 2.0),
            "exit_spread_pct": ("Exit Spread %", 0.02),
        },
    },
    "cash_and_carry": {
        "icon": "CNC", "name": "Cash and Carry",
        "desc": "Buy spot + short futures for funding",
        "params": {
            "min_funding_apr_pct": ("Min Funding APR %", 15.0),
            "max_basis_spread_pct": ("Max Basis %", 0.5),
        },
    },
    "funding_arbitrage": {
        "icon": "FND", "name": "Funding Arbitrage",
        "desc": "Funding rate diff between exchanges",
        "params": {
            "funding_arb_min_diff_pct": ("Min Diff %", 0.01),
            "funding_arb_max_spread_cost_bps": ("Max Spread bps", 5.0),
            "funding_arb_target_profit_bps": ("Target Profit bps", 3.0),
        },
    },
    "triangular": {
        "icon": "TRI", "name": "Triangular Arbitrage",
        "desc": "3-pair cycle on single exchange",
        "params": {
            "triangular_min_profit_bps": ("Min Profit bps", 5.0),
            "triangular_fee_per_leg_pct": ("Fee/Leg %", 0.1),
            "triangular_cooldown_sec": ("Cooldown sec", 30),
        },
    },
    "pairs_trading": {
        "icon": "PRS", "name": "Pairs Trading",
        "desc": "Statistical mean-reversion pairs",
        "params": {
            "pairs_entry_zscore": ("Entry Z-score", 2.0),
            "pairs_exit_zscore": ("Exit Z-score", 0.5),
            "pairs_min_history": ("Min History", 100),
            "pairs_min_profit_bps": ("Min Profit bps", 10.0),
        },
    },
    "funding_harvesting": {
        "icon": "HRV", "name": "Funding Harvesting",
        "desc": "Collect high funding rates",
        "params": {
            "funding_harvest_min_rate_pct": ("Min Rate %", 0.03),
            "funding_harvest_min_apr": ("Min APR %", 20.0),
            "funding_harvest_max_basis_pct": ("Max Basis %", 0.3),
        },
    },
}

# Tracks pending parameter edits: user_id -> {"sid": ..., "pname": ...}
_pending_edit: Dict[int, Dict[str, str]] = {}

# Default enabled strategies
_DEFAULT_ENABLED = ["futures_cross_exchange", "cash_and_carry", "funding_arbitrage"]


# ---------------------------------------------------------------------------
# Load / Save helpers
# ---------------------------------------------------------------------------
def _load() -> Dict[str, Any]:
    if SF.exists():
        try:
            return json.loads(SF.read_text())
        except Exception:
            pass
    return {}


def _save(d: Dict[str, Any]) -> None:
    SF.parent.mkdir(parents=True, exist_ok=True)
    SF.write_text(json.dumps(d, indent=2, ensure_ascii=False))


def _enabled_list(settings: Dict) -> list:
    return settings.get("enabled", list(_DEFAULT_ENABLED))


def _is_enabled(sid: str, settings: Dict) -> bool:
    return sid in _enabled_list(settings)


def _get_param(sid: str, pname: str, settings: Dict) -> Any:
    return settings.get("params", {}).get(sid, {}).get(
        pname, STRATS[sid]["params"][pname][1]
    )


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------
def _strat_list_kb(settings: Dict) -> IKM:
    """Build a keyboard listing all strategies with ON/OFF marks."""
    rows = []
    for sid, meta in STRATS.items():
        on = _is_enabled(sid, settings)
        mark = "[ON]" if on else "[OFF]"
        rows.append([
            IKB(text=f"{meta['icon']} {meta['name']} {mark}",
                callback_data=f"arb_strat_detail:{sid}"),
        ])
    rows.append([IKB(text="Reset all defaults", callback_data="arb_strat_reset")])
    rows.append([IKB(text="<< Back", callback_data="arb_menu")])
    return IKM(inline_keyboard=rows)


def _detail_kb(sid: str, settings: Dict) -> IKM:
    """Build a keyboard for a single strategy: toggle + params + back."""
    meta = STRATS[sid]
    on = _is_enabled(sid, settings)
    toggle_txt = "Disable" if on else "Enable"
    rows = [
        [IKB(text=f"{toggle_txt} strategy",
             callback_data=f"arb_strat_toggle:{sid}")],
    ]
    for pname, (label, default) in meta["params"].items():
        val = _get_param(sid, pname, settings)
        rows.append([
            IKB(text=f"{label}: {val}",
                callback_data=f"arb_param_edit:{sid}:{pname}"),
        ])
    rows.append([IKB(text="Reset this strategy", callback_data=f"arb_strat_reset_one:{sid}")])
    rows.append([IKB(text="<< Strategies", callback_data="arb_strategies")])
    return IKM(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Safe message edit (suppresses "message is not modified" errors)
# ---------------------------------------------------------------------------
async def _safe_edit(cb: types.CallbackQuery, text: str, kb: IKM | None = None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass


def _detail_text(sid: str, settings: Dict) -> str:
    """Render the detail view text for a strategy."""
    meta = STRATS[sid]
    on = _is_enabled(sid, settings)
    status = "ENABLED" if on else "DISABLED"
    lines = [
        f"<b>{meta['icon']} {meta['name']}</b>",
        f"<i>{meta['desc']}</i>",
        f"Status: <b>{status}</b>",
        "",
        "<b>Parameters:</b>",
    ]
    for pname, (label, default) in meta["params"].items():
        val = _get_param(sid, pname, settings)
        changed = " *" if val != default else ""
        lines.append(f"  {label}: <code>{val}</code>{changed}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------
async def cb_strategies(cb: types.CallbackQuery) -> None:
    """Show the strategy list with enable/disable indicators."""
    await cb.answer()
    s = _load()
    await _safe_edit(cb, "<b>Strategy settings</b>\nTap a strategy to configure:", _strat_list_kb(s))


async def cb_strat_detail(cb: types.CallbackQuery) -> None:
    """Show detail view for a single strategy."""
    await cb.answer()
    sid = cb.data.split(":")[1]
    if sid not in STRATS:
        return
    s = _load()
    text = _detail_text(sid, s)
    await _safe_edit(cb, text, _detail_kb(sid, s))


async def cb_strat_toggle(cb: types.CallbackQuery) -> None:
    """Toggle a strategy enabled/disabled."""
    await cb.answer()
    sid = cb.data.split(":")[1]
    if sid not in STRATS:
        return
    s = _load()
    enabled = _enabled_list(s)
    if sid in enabled:
        enabled.remove(sid)
        action = "disabled"
    else:
        enabled.append(sid)
        action = "enabled"
    s["enabled"] = enabled
    _save(s)
    log.info("Strategy %s %s by user %s", sid, action, cb.from_user.id)
    text = _detail_text(sid, s)
    await _safe_edit(cb, text, _detail_kb(sid, s))


async def cb_param_edit(cb: types.CallbackQuery) -> None:
    """Prompt user to enter a new value for a strategy parameter."""
    await cb.answer()
    parts = cb.data.split(":")
    if len(parts) < 3:
        return
    sid, pname = parts[1], parts[2]
    if sid not in STRATS or pname not in STRATS[sid]["params"]:
        return
    label, default = STRATS[sid]["params"][pname]
    s = _load()
    current = _get_param(sid, pname, s)
    _pending_edit[cb.from_user.id] = {"sid": sid, "pname": pname}
    text = (
        f"<b>Edit: {label}</b>\n\n"
        f"Strategy: {STRATS[sid]['name']}\n"
        f"Current value: <code>{current}</code>\n"
        f"Default: <code>{default}</code>\n\n"
        "Send the new numeric value as a message.\n"
        "Send <code>default</code> to reset to default."
    )
    kb = IKM(inline_keyboard=[
        [IKB(text="Cancel", callback_data=f"arb_strat_detail:{sid}")],
    ])
    await _safe_edit(cb, text, kb)


async def cb_strat_reset(cb: types.CallbackQuery) -> None:
    """Reset ALL strategy settings to defaults."""
    await cb.answer("Settings reset to defaults")
    _save({})
    log.info("All arb settings reset by user %s", cb.from_user.id)
    s = _load()
    await _safe_edit(cb, "<b>Strategy settings</b> (reset to defaults):", _strat_list_kb(s))


async def cb_strat_reset_one(cb: types.CallbackQuery) -> None:
    """Reset a single strategy params to defaults."""
    await cb.answer("Strategy reset to defaults")
    sid = cb.data.split(":")[1]
    if sid not in STRATS:
        return
    s = _load()
    if "params" in s and sid in s["params"]:
        del s["params"][sid]
        _save(s)
    log.info("Strategy %s settings reset by user %s", sid, cb.from_user.id)
    text = _detail_text(sid, s)
    await _safe_edit(cb, text, _detail_kb(sid, s))


async def process_param_value(msg: types.Message) -> None:
    """Process a text message as a new parameter value."""
    uid = msg.from_user.id
    pending = _pending_edit.pop(uid, None)
    if not pending:
        return
    sid = pending["sid"]
    pname = pending["pname"]
    if sid not in STRATS or pname not in STRATS[sid]["params"]:
        await msg.answer("Invalid parameter reference.")
        return

    label, default = STRATS[sid]["params"][pname]
    raw = msg.text.strip()

    # Handle default keyword
    if raw.lower() == "default":
        s = _load()
        params = s.setdefault("params", {})
        strat_params = params.get(sid, {})
        strat_params.pop(pname, None)
        if strat_params:
            params[sid] = strat_params
        elif sid in params:
            del params[sid]
        _save(s)
        await msg.answer(
            f"<b>{label}</b> reset to default: <code>{default}</code>",
            parse_mode="HTML",
        )
        return

    # Try to parse numeric value
    try:
        if isinstance(default, int):
            value = int(raw)
        else:
            value = float(raw)
    except ValueError:
        _pending_edit[uid] = pending
        await msg.answer(
            "Invalid value. Please send a number (or <code>default</code>).",
            parse_mode="HTML",
        )
        return

    # Validate non-negative
    if value < 0:
        _pending_edit[uid] = pending
        await msg.answer("Value must be non-negative. Try again.")
        return

    s = _load()
    params = s.setdefault("params", {})
    strat_params = params.setdefault(sid, {})
    strat_params[pname] = value
    _save(s)

    log.info("Param %s.%s set to %s by user %s", sid, pname, value, uid)
    await msg.answer(
        f"<b>{label}</b> set to <code>{value}</code>",
        parse_mode="HTML",
    )


def has_pending_edit(user_id: int) -> bool:
    """Check whether a user has a pending parameter edit."""
    return user_id in _pending_edit


def cancel_pending_edit(user_id: int) -> None:
    """Cancel any pending parameter edit for a user."""
    _pending_edit.pop(user_id, None)


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------
def register_handlers(dp: Dispatcher) -> None:
    """Register all arb-settings callback handlers on the given dispatcher."""
    dp.callback_query.register(
        cb_strategies, lambda c: c.data == "arb_strategies")
    dp.callback_query.register(
        cb_strat_detail, lambda c: c.data and c.data.startswith("arb_strat_detail:"))
    dp.callback_query.register(
        cb_strat_toggle, lambda c: c.data and c.data.startswith("arb_strat_toggle:"))
    dp.callback_query.register(
        cb_param_edit, lambda c: c.data and c.data.startswith("arb_param_edit:"))
    dp.callback_query.register(
        cb_strat_reset, lambda c: c.data == "arb_strat_reset")
    dp.callback_query.register(
        cb_strat_reset_one, lambda c: c.data and c.data.startswith("arb_strat_reset_one:"))
