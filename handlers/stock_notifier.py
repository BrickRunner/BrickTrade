"""
Stock Trading Telegram Notifier — rich notifications, trade plans, and reports.

Notification categories (each toggleable):
  trade_entry, trade_exit, signal, risk_alert, session,
  plan_alert, daily_report, position_update
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from aiogram import Bot

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))
_PLANS_FILE = os.path.join("data", "stock_trade_plans.json")
_PREFS_FILE = os.path.join("data", "stock_notify_prefs.json")

# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------

_DEFAULT_PREFS: Dict[str, bool] = {
    "trade_entry": True,
    "trade_exit": True,
    "signal": True,
    "risk_alert": True,
    "session": True,
    "plan_alert": True,
    "daily_report": True,
    "position_update": True,
    "quiet_hours": False,
}

_PREF_LABELS: Dict[str, str] = {
    "trade_entry": "📥 Открытие позиций",
    "trade_exit": "📤 Закрытие позиций",
    "signal": "📡 Сигналы",
    "risk_alert": "🚨 Риск-алерты",
    "session": "🔔 Сессии MOEX",
    "plan_alert": "🎯 Планы",
    "daily_report": "📊 Дневной отчёт",
    "position_update": "📏 Обновления позиций",
    "quiet_hours": "🌙 Тихие часы (23-07)",
}


def _load_prefs() -> Dict[str, bool]:
    try:
        with open(_PREFS_FILE) as f:
            saved = json.load(f)
        prefs = dict(_DEFAULT_PREFS)
        prefs.update(saved)
        return prefs
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_PREFS)


def _save_prefs(prefs: Dict[str, bool]) -> None:
    os.makedirs(os.path.dirname(_PREFS_FILE), exist_ok=True)
    with open(_PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


# ---------------------------------------------------------------------------
# Trade plans
# ---------------------------------------------------------------------------

class PlanStatus(str, Enum):
    ACTIVE = "active"
    TRIGGERED = "triggered"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class TradePlan:
    """User-defined trade plan with entry/exit zones."""
    plan_id: str
    ticker: str
    side: str
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit: float
    note: str = ""
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    triggered_at: float = 0.0
    last_alert_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TradePlan":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class TradePlanManager:
    """Persists and evaluates trade plans against live prices."""

    def __init__(self) -> None:
        self._plans: Dict[str, TradePlan] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(_PLANS_FILE) as f:
                raw = json.load(f)
            for d in raw:
                plan = TradePlan.from_dict(d)
                self._plans[plan.plan_id] = plan
            logger.info("stock_plans: loaded %d plans", len(self._plans))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        os.makedirs(os.path.dirname(_PLANS_FILE), exist_ok=True)
        with open(_PLANS_FILE, "w") as f:
            json.dump([p.to_dict() for p in self._plans.values()], f, indent=2, ensure_ascii=False)

    def add(self, plan: TradePlan) -> None:
        self._plans[plan.plan_id] = plan
        self._save()

    def remove(self, plan_id: str) -> Optional[TradePlan]:
        plan = self._plans.pop(plan_id, None)
        if plan:
            self._save()
        return plan

    def cancel(self, plan_id: str) -> Optional[TradePlan]:
        plan = self._plans.get(plan_id)
        if plan and plan.status == PlanStatus.ACTIVE:
            plan.status = PlanStatus.CANCELLED
            self._save()
        return plan

    def get(self, plan_id: str) -> Optional[TradePlan]:
        return self._plans.get(plan_id)

    def list_active(self) -> List[TradePlan]:
        now = time.time()
        result: List[TradePlan] = []
        changed = False
        for p in self._plans.values():
            if p.status == PlanStatus.ACTIVE:
                if p.expires_at > 0 and now > p.expires_at:
                    p.status = PlanStatus.EXPIRED
                    changed = True
                    continue
                result.append(p)
        if changed:
            self._save()
        return result

    def list_all(self) -> List[TradePlan]:
        return list(self._plans.values())

    def check_prices(self, prices: Dict[str, float]) -> List[TradePlan]:
        """Check live prices against active plans, return newly triggered."""
        triggered: List[TradePlan] = []
        now = time.time()
        for plan in self.list_active():
            price = prices.get(plan.ticker, 0)
            if price <= 0:
                continue
            if now - plan.last_alert_at < 300:
                continue
            if plan.entry_low <= price <= plan.entry_high:
                plan.status = PlanStatus.TRIGGERED
                plan.triggered_at = now
                plan.last_alert_at = now
                triggered.append(plan)
        if triggered:
            self._save()
        return triggered

    def mark_executed(self, plan_id: str) -> None:
        plan = self._plans.get(plan_id)
        if plan:
            plan.status = PlanStatus.EXECUTED
            self._save()


# ---------------------------------------------------------------------------
# Trade history for daily reports
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    """Lightweight record of a completed trade."""
    ticker: str
    side: str
    strategy: str
    entry_price: float
    exit_price: float
    quantity_lots: int
    lot_size: int
    pnl: float
    exit_reason: str
    duration_sec: float
    timestamp: float = field(default_factory=time.time)


class TradeHistory:
    """In-memory trade history for the current day."""

    def __init__(self) -> None:
        self._trades: List[TradeRecord] = []
        self._day_mark: str = time.strftime("%Y-%m-%d")

    def _maybe_reset(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day_mark:
            self._trades.clear()
            self._day_mark = today

    def add(self, record: TradeRecord) -> None:
        self._maybe_reset()
        self._trades.append(record)

    @property
    def today_trades(self) -> List[TradeRecord]:
        self._maybe_reset()
        return list(self._trades)

    @property
    def today_pnl(self) -> float:
        return sum(t.pnl for t in self.today_trades)

    @property
    def today_count(self) -> int:
        return len(self.today_trades)

    def winners(self) -> int:
        return sum(1 for t in self.today_trades if t.pnl > 0)

    def losers(self) -> int:
        return sum(1 for t in self.today_trades if t.pnl < 0)


# ---------------------------------------------------------------------------
# StockNotifier
# ---------------------------------------------------------------------------

_SIDE_RU = {"buy": "ПОКУПКА", "sell": "ПРОДАЖА"}
_SIDE_EMOJI = {"buy": "🟢", "sell": "🔴"}
_EXIT_REASON_RU = {
    "stop_loss": "🛑 Стоп-лосс",
    "take_profit": "🎯 Тейк-профит",
    "trailing_stop": "📏 Трейлинг-стоп",
    "time_stop": "⏰ Тайм-стоп",
    "session_close": "🔔 Закрытие сессии",
    "emergency": "🚨 Экстренное",
    "manual": "👤 Ручное",
}


class StockNotifier:
    """Sends structured Telegram notifications for stock trading events."""

    def __init__(self, bot: Bot, user_id: int) -> None:
        self._bot = bot
        self._user_id = user_id
        self._prefs = _load_prefs()
        self.plans = TradePlanManager()
        self.history = TradeHistory()
        self._last_session_type: str = ""
        self._daily_report_sent: str = ""

    # ---- Preferences ----

    @property
    def prefs(self) -> Dict[str, bool]:
        return dict(self._prefs)

    def toggle_pref(self, key: str) -> bool:
        if key in self._prefs:
            self._prefs[key] = not self._prefs[key]
            _save_prefs(self._prefs)
        return self._prefs.get(key, True)

    def set_pref(self, key: str, value: bool) -> None:
        if key in self._prefs:
            self._prefs[key] = value
            _save_prefs(self._prefs)

    def _is_quiet_hours(self) -> bool:
        if not self._prefs.get("quiet_hours", False):
            return False
        now_msk = datetime.now(tz=_MSK)
        return now_msk.hour < 7 or now_msk.hour >= 23

    def _should_notify(self, category: str) -> bool:
        if not self._prefs.get(category, True):
            return False
        if self._is_quiet_hours() and category not in ("risk_alert", "trade_exit"):
            return False
        return True

    # ---- Core send ----

    async def _send(self, text: str, category: str = "signal") -> None:
        if not self._should_notify(category):
            return
        try:
            await self._bot.send_message(
                self._user_id, text, parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("stock_notifier: send failed: %s", exc)

    # ---- Trade entry ----

    async def notify_trade_entry(
        self, ticker: str, side: str, strategy: str,
        quantity_lots: int, lot_size: int, fill_price: float,
        stop_loss: float, take_profit: float,
        confidence: float = 0.0, expected_edge: float = 0.0,
        is_dry_run: bool = False,
    ) -> None:
        emoji = _SIDE_EMOJI.get(side, "⚪")
        side_ru = _SIDE_RU.get(side, side)
        shares = quantity_lots * lot_size
        cost = fill_price * shares
        dry_tag = " <i>(dry-run)</i>" if is_dry_run else ""
        now_msk = datetime.now(tz=_MSK).strftime("%H:%M:%S")
        risk_pct = abs(fill_price - stop_loss) / fill_price * 100 if fill_price else 0
        reward_pct = abs(take_profit - fill_price) / fill_price * 100 if fill_price else 0
        rr = reward_pct / risk_pct if risk_pct > 0 else 0

        text = (
            f"{emoji} <b>ОТКРЫТА ПОЗИЦИЯ</b>{dry_tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>{ticker}</b> | {side_ru}\n"
            f"💰 Цена: <b>{fill_price:,.2f} ₽</b>\n"
            f"📦 {quantity_lots} лот x {lot_size} = {shares} шт. ({cost:,.0f} ₽)\n\n"
            f"🛡 <b>Риск:</b>\n"
            f"  🛑 SL: {stop_loss:,.2f} ₽ (-{risk_pct:.1f}%)\n"
            f"  🎯 TP: {take_profit:,.2f} ₽ (+{reward_pct:.1f}%)\n"
            f"  📊 R:R = 1:{rr:.1f}\n\n"
            f"🧠 <code>{strategy}</code> | conf={confidence:.0%} edge={expected_edge:.2f}%\n"
            f"🕐 {now_msk}"
        )
        await self._send(text, "trade_entry")

    # ---- Trade exit ----

    async def notify_trade_exit(
        self, ticker: str, side: str, strategy: str,
        quantity_lots: int, lot_size: int,
        entry_price: float, exit_price: float,
        pnl: float, exit_reason: str, duration_sec: float,
        is_dry_run: bool = False,
    ) -> None:
        pnl_emoji = "✅" if pnl >= 0 else "❌"
        reason_ru = _EXIT_REASON_RU.get(exit_reason, exit_reason)
        shares = quantity_lots * lot_size
        pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price else 0
        if side == "sell":
            pnl_pct = -pnl_pct
        dry_tag = " <i>(dry-run)</i>" if is_dry_run else ""
        now_msk = datetime.now(tz=_MSK).strftime("%H:%M:%S")
        dur_min = int(duration_sec / 60)
        dur_str = f"{dur_min} мин" if dur_min < 60 else f"{dur_min // 60}ч {dur_min % 60}м"

        text = (
            f"{pnl_emoji} <b>ЗАКРЫТА ПОЗИЦИЯ</b>{dry_tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>{ticker}</b> | {_SIDE_RU.get(side, side)}\n"
            f"📥 {entry_price:,.2f} → 📤 {exit_price:,.2f} ₽ ({shares} шт.)\n\n"
            f"💰 <b>P&L: {pnl:+,.1f} ₽ ({pnl_pct:+.2f}%)</b>\n"
            f"📌 {reason_ru} | ⏱ {dur_str}\n"
            f"🧠 <code>{strategy}</code>\n"
            f"🕐 {now_msk}"
        )
        await self._send(text, "trade_exit")

        self.history.add(TradeRecord(
            ticker=ticker, side=side, strategy=strategy,
            entry_price=entry_price, exit_price=exit_price,
            quantity_lots=quantity_lots, lot_size=lot_size,
            pnl=pnl, exit_reason=exit_reason, duration_sec=duration_sec,
        ))

    # ---- Signal (monitoring mode) ----

    async def notify_signal(
        self, ticker: str, side: str, strategy: str,
        confidence: float, expected_edge: float, price: float,
    ) -> None:
        emoji = _SIDE_EMOJI.get(side, "⚪")
        side_ru = _SIDE_RU.get(side, side)
        now_msk = datetime.now(tz=_MSK).strftime("%H:%M:%S")
        strength = "🔥" if confidence >= 0.3 else "📊" if confidence >= 0.2 else "💤"

        text = (
            f"📡 <b>СИГНАЛ</b> {strength}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} <b>{ticker}</b> → {side_ru}\n"
            f"💰 Цена: {price:,.2f} ₽\n"
            f"🧠 <code>{strategy}</code>\n"
            f"📈 conf={confidence:.0%} | edge={expected_edge:.2f}%\n"
            f"🕐 {now_msk}"
        )
        await self._send(text, "signal")

    # ---- Trailing stop update ----

    async def notify_trailing_stop_update(
        self, ticker: str, side: str,
        old_sl: float, new_sl: float, current_price: float,
    ) -> None:
        change_pct = abs(new_sl - old_sl) / current_price * 100 if current_price else 0
        if change_pct < 0.5:
            return
        direction = "⬆️" if new_sl > old_sl else "⬇️"
        text = (
            f"📏 <b>Трейлинг-стоп</b>\n"
            f"📋 {ticker} | {_SIDE_RU.get(side, side)}\n"
            f"  {old_sl:,.2f} {direction} <b>{new_sl:,.2f} ₽</b>\n"
            f"  Цена: {current_price:,.2f} ₽"
        )
        await self._send(text, "position_update")

    # ---- Risk alerts ----

    async def notify_kill_switch(self, reason: str) -> None:
        text = (
            f"🚨 <b>KILL-SWITCH АКТИВИРОВАН</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ {reason}\n"
            f"🛑 Новые сделки заблокированы.\n"
            f"Для сброса перезапустите движок."
        )
        await self._send(text, "risk_alert")

    async def notify_drawdown_warning(self, current_dd_pct: float, max_dd_pct: float) -> None:
        pct_used = current_dd_pct / max_dd_pct * 100 if max_dd_pct else 0
        text = (
            f"⚠️ <b>Просадка {current_dd_pct:.2f}%</b> (лимит {max_dd_pct:.2f}%)\n"
            f"Использовано: {pct_used:.0f}% от лимита"
        )
        await self._send(text, "risk_alert")

    async def notify_daily_trade_limit(self, count: int, limit: int) -> None:
        status = "🛑 Лимит!" if count >= limit else f"⏳ Осталось: {limit - count}"
        text = f"⚠️ <b>Сделки: {count} / {limit}</b> {status}"
        await self._send(text, "risk_alert")

    # ---- Session notifications ----

    async def notify_session_change(self, new_session: str, tickers: List[str]) -> None:
        if new_session == self._last_session_type:
            return
        self._last_session_type = new_session
        names = {
            "morning": "🌅 Утренняя сессия",
            "main": "🏛 Основная сессия",
            "evening": "🌆 Вечерняя сессия",
            "closed": "🌙 Биржа закрыта",
        }
        name = names.get(new_session, new_session)
        now_msk = datetime.now(tz=_MSK).strftime("%H:%M")
        tickers_str = ", ".join(tickers[:5])
        if len(tickers) > 5:
            tickers_str += f" +{len(tickers) - 5}"

        text = f"{name}\n🕐 {now_msk} МСК | 📋 {tickers_str}"
        if new_session == "closed":
            await self._send(text, "session")
            await self.send_daily_report()
        else:
            await self._send(text, "session")

    # ---- Trade plan alerts ----

    async def check_and_notify_plans(self, prices: Dict[str, float]) -> List[TradePlan]:
        triggered = self.plans.check_prices(prices)
        for plan in triggered:
            price = prices.get(plan.ticker, 0)
            emoji = _SIDE_EMOJI.get(plan.side, "⚪")
            side_ru = _SIDE_RU.get(plan.side, plan.side)
            now_msk = datetime.now(tz=_MSK).strftime("%H:%M:%S")
            text = (
                f"🎯 <b>ПЛАН СРАБОТАЛ!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{emoji} <b>{plan.ticker}</b> → {side_ru}\n"
                f"💰 Цена: <b>{price:,.2f} ₽</b>\n"
                f"📏 Зона: {plan.entry_low:,.2f} – {plan.entry_high:,.2f} ₽\n"
                f"🛑 SL: {plan.stop_loss:,.2f} | 🎯 TP: {plan.take_profit:,.2f}\n"
            )
            if plan.note:
                text += f"📝 {plan.note}\n"
            text += f"🕐 {now_msk}"
            await self._send(text, "plan_alert")
        return triggered

    # ---- Daily report ----

    async def send_daily_report(
        self, portfolio_value: float = 0, cash_available: float = 0,
        open_positions: int = 0,
    ) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._daily_report_sent == today:
            return
        self._daily_report_sent = today

        trades = self.history.today_trades
        total_pnl = self.history.today_pnl
        winners = self.history.winners()
        losers = self.history.losers()
        total = self.history.today_count
        winrate = winners / total * 100 if total > 0 else 0
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        now_msk = datetime.now(tz=_MSK).strftime("%d.%m.%Y %H:%M")

        lines = [
            "📊 <b>ДНЕВНОЙ ОТЧЁТ</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📅 {now_msk} МСК",
            "",
        ]

        if portfolio_value > 0:
            lines.append("💼 <b>Портфель:</b>")
            lines.append(f"  Стоимость: {portfolio_value:,.0f} ₽")
            lines.append(f"  Свободные: {cash_available:,.0f} ₽")
            lines.append(f"  Открытых: {open_positions}")
            lines.append("")

        lines.append(f"{pnl_emoji} <b>Результат дня:</b>")
        lines.append(f"  P&L: <b>{total_pnl:+,.1f} ₽</b>")
        lines.append(f"  Сделок: {total}")

        if total > 0:
            lines.append(f"  ✅ Прибыльных: {winners} | ❌ Убыточных: {losers}")
            lines.append(f"  📊 Winrate: {winrate:.0f}%")
            lines.append("")

            # Per-strategy
            by_strategy: Dict[str, List[TradeRecord]] = {}
            for t in trades:
                by_strategy.setdefault(t.strategy, []).append(t)
            lines.append("📋 <b>По стратегиям:</b>")
            for strat, strades in by_strategy.items():
                spnl = sum(t.pnl for t in strades)
                lines.append(f"  <code>{strat}</code>: {len(strades)} сд., {spnl:+,.1f} ₽")

            # Best and worst trade
            best = max(trades, key=lambda t: t.pnl)
            worst = min(trades, key=lambda t: t.pnl)
            lines.append("")
            lines.append(f"🏆 Лучшая: {best.ticker} {best.pnl:+,.1f} ₽")
            lines.append(f"💀 Худшая: {worst.ticker} {worst.pnl:+,.1f} ₽")
        else:
            lines.append("  Сделок не было")

        # Active plans summary
        active_plans = self.plans.list_active()
        if active_plans:
            lines.append("")
            lines.append(f"🎯 Активных планов: {len(active_plans)}")

        text = "\n".join(lines)
        await self._send(text, "daily_report")

    # ---- Force daily report (user-triggered, ignores date check) ----

    async def send_report_now(
        self, portfolio_value: float = 0, cash_available: float = 0,
        open_positions: int = 0,
    ) -> None:
        """Send report immediately, bypassing the once-a-day check."""
        self._daily_report_sent = ""  # Reset so send_daily_report actually sends
        await self.send_daily_report(portfolio_value, cash_available, open_positions)
