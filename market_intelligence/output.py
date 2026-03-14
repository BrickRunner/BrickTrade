# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List

from market_intelligence.models import DataHealthStatus, MarketIntelligenceReport, MarketRegime


def _fmt_msk(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Moscow"))
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def _regime_ru(regime: MarketRegime) -> str:
    mapping = {
        MarketRegime.RANGE: "флэт",
        MarketRegime.TREND_UP: "восходящий тренд",
        MarketRegime.TREND_DOWN: "нисходящий тренд",
        MarketRegime.OVERHEATED: "перегрев",
        MarketRegime.PANIC: "паника",
        MarketRegime.HIGH_VOLATILITY: "высокая волатильность",
    }
    return mapping.get(regime, "нейтральный режим")


def _health_ru(status: DataHealthStatus) -> str:
    if status == DataHealthStatus.OK:
        return "данные в норме"
    if status == DataHealthStatus.PARTIAL:
        return "частично доступно"
    return "критически неполные"


def _local_vol_ru(state: str | None) -> str:
    mapping = {
        "expanding": "расширяется",
        "contracting": "сжимается",
        "unavailable": "недоступно",
    }
    return mapping.get(str(state or "unavailable").lower(), "недоступно")


def _momentum_ru(state: str | None) -> str:
    mapping = {
        "neutral": "нейтральный",
        "bullish": "бычий",
        "bearish": "медвежий",
        "unavailable": "недоступно",
    }
    return mapping.get(str(state or "unavailable").lower(), "недоступно")


def _bias_ru(bias: str) -> str:
    return {"short": "шорт", "long": "лонг", "neutral": "нейтрально"}.get(bias, bias)


def _arrow(v: float) -> str:
    if v > 0:
        return "↑"
    if v < 0:
        return "↓"
    return "→"


def _trading_conditions(regime: MarketRegime, adx: float | None, local_vol_state: str | None) -> tuple[str, str, str, str]:
    if regime == MarketRegime.RANGE:
        market_state = "нейтральное"
    elif regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}:
        market_state = "трендовое"
    elif regime in {MarketRegime.PANIC, MarketRegime.HIGH_VOLATILITY}:
        market_state = "стрессовое"
    else:
        market_state = "перегретое"

    if adx is None:
        trend_strength = "не определена"
    elif adx < 20:
        trend_strength = "слабая"
    elif adx < 30:
        trend_strength = "умеренная"
    else:
        trend_strength = "сильная"

    vol_state = str(local_vol_state or "").lower()
    if vol_state == "expanding" and (adx or 0) >= 25:
        breakout_prob = "повышенная"
    elif vol_state == "contracting":
        breakout_prob = "низкая"
    else:
        breakout_prob = "средняя"

    if market_state in {"стрессовое", "перегретое"}:
        recommendation = "Снизить риск и избегать агрессивных входов до стабилизации рынка."
    elif market_state == "нейтральное":
        recommendation = "Избегать агрессивных входов до роста волатильности и подтверждения импульса."
    else:
        recommendation = "Работать по тренду только при подтвержденных сигналах и фиксированном риске."

    return market_state, trend_strength, breakout_prob, recommendation


def format_human_report(report: MarketIntelligenceReport, top_n: int = 3) -> str:
    payload = report.payload
    global_block = payload.get("global", {})
    metrics = global_block.get("metrics", {})
    local_ctx = payload.get("local", {}).get("context", {})
    deltas = payload.get("dynamic_deltas", {})

    price = metrics.get("current_price")
    range_low = metrics.get("range_low")
    range_high = metrics.get("range_high")
    volatility_brief = metrics.get("volatility_brief") or "оценка ограничена"

    lines: List[str] = []
    lines.append("Обновление состояния рынка")
    lines.append(f"Время (MSK): {_fmt_msk(report.timestamp)}")
    lines.append(f"Таймфреймы: Глобальный {report.global_timeframe} | Локальный {report.local_timeframe}")
    lines.append("")

    lines.append(f"Состояние данных: {_health_ru(report.data_health_status)}")
    if report.data_health_status != DataHealthStatus.OK or report.data_health_warnings:
        lines.append("Часть метрик временно недоступна, анализ проводится с пониженной точностью.")
    lines.append("")

    lines.append(f"ГЛОБАЛЬНЫЙ АНАЛИЗ ({global_block.get('symbol', 'BTCUSDT')} {report.global_timeframe})")
    lines.append(f"Цена: {price:.6f}" if price is not None else "Цена: недоступно")
    if range_low is not None and range_high is not None:
        lines.append(f"Диапазон движения: {range_low:.6f} - {range_high:.6f}")
    else:
        lines.append("Диапазон движения: недоступно")
    lines.append(
        f"Режим рынка: {_regime_ru(report.global_regime.regime)} "
        f"(вероятность {report.global_regime.confidence * 100:.0f}%)"
    )
    lines.append(f"Волатильность: {volatility_brief}")
    lines.append("")

    lines.append(f"ЛОКАЛЬНАЯ СИТУАЦИЯ ({report.local_timeframe})")
    adx = local_ctx.get("adx")
    if adx is None:
        lines.append("ADX: недоступно (недостаточно свечей)")
    else:
        lines.append(f"ADX: {adx:.2f}")
    lines.append(f"Волатильность: {_local_vol_ru(local_ctx.get('volatility_state'))}")
    lines.append(f"Моментум: {_momentum_ru(local_ctx.get('momentum_bias'))}")
    lines.append("")

    lines.append("ИЗМЕНЕНИЯ РЫНКА")
    if deltas.get("first_cycle"):
        lines.append("Изменение режима: недоступно (недостаточно истории)")
        lines.append("Уверенность режима: недоступно (недостаточно истории)")
        lines.append("Динамика ATR/OI/фандинга: временно недоступна")
    else:
        lines.append(f"Изменение режима: {'да' if deltas.get('regime_changed') else 'нет'}")
        conf_delta = deltas.get("confidence_change")
        if report.data_health_status != DataHealthStatus.OK or conf_delta is None:
            lines.append("Уверенность режима: недоступно (недостаточно истории)")
        else:
            lines.append(f"Уверенность режима: {_arrow(float(conf_delta))} {float(conf_delta):+.3f}")

        atr_d = deltas.get("volatility_change_pct")
        oi_d = deltas.get("oi_change_pct")
        fnd_d = deltas.get("funding_change_pct")
        if atr_d is None or oi_d is None or fnd_d is None:
            lines.append("Динамика ATR/OI/фандинга: временно недоступна")
        else:
            lines.append(
                "Динамика ATR/OI/фандинга: "
                f"ATR {_arrow(float(atr_d))} {float(atr_d):+.3f}%, "
                f"OI {_arrow(float(oi_d))} {float(oi_d):+.3f}%, "
                f"Фандинг {_arrow(float(fnd_d))} {float(fnd_d):+.4f}%"
            )
    lines.append("")

    lines.append("УСЛОВИЯ ТОРГОВЛИ")
    market_state, trend_strength, breakout_prob, recommendation = _trading_conditions(
        report.global_regime.regime,
        adx,
        local_ctx.get("volatility_state"),
    )
    lines.append(f"Состояние рынка: {market_state}")
    lines.append(f"Сила тренда: {trend_strength}")
    lines.append(f"Вероятность пробоя: {breakout_prob}")
    # Multi-timeframe convergence for BTC.
    conv_scores = payload.get("convergence_scores", {})
    btc_sym = global_block.get("symbol", "BTCUSDT")
    conv = conv_scores.get(btc_sym, 1.0)
    if abs(conv - 1.0) > 0.01:
        if conv >= 1.05:
            conv_label = "высокая"
        elif conv >= 0.95:
            conv_label = "нейтральная"
        else:
            conv_label = "низкая (расхождение таймфреймов)"
        lines.append(f"Конвергенция таймфреймов: {conv_label} ({conv:.2f})")
    # Orderbook pressure (BTC)
    btc_features = payload.get("features", {}).get(btc_sym, {})
    ob_imb = btc_features.get("orderbook_imbalance")
    if ob_imb is not None:
        ob_val = float(ob_imb)
        if ob_val > 0.15:
            lines.append(f"Давление стакана: покупатели ({ob_val:+.2f})")
        elif ob_val < -0.15:
            lines.append(f"Давление стакана: продавцы ({ob_val:+.2f})")
        else:
            lines.append(f"Давление стакана: нейтральное ({ob_val:+.2f})")
    lines.append("Рекомендация: " + recommendation)
    lines.append("")

    # FIX F1: Microstructure section (cascade risk, liquidity withdrawal, funding mean reversion)
    cascade_risk = btc_features.get("cascade_risk")
    liq_withdrawal = btc_features.get("liquidity_withdrawal")
    funding_mr = btc_features.get("funding_mean_reversion_signal")

    micro_lines = []
    if cascade_risk is not None and float(cascade_risk) > 0.15:
        stage = int(float(btc_features.get("cascade_stage") or 0))
        stage_names = {0: "нет", 1: "ранний", 2: "развивающийся", 3: "АКТИВНЫЙ"}
        micro_lines.append(f"Каскад ликвидаций: {stage_names.get(stage, '?')} (риск {float(cascade_risk):.0%})")

    if liq_withdrawal is not None and float(liq_withdrawal) > 0.3:
        micro_lines.append(f"Отток ликвидности: {float(liq_withdrawal):.0%}")

    if funding_mr is not None and float(funding_mr) > 0.3:
        micro_lines.append(f"Сигнал mean-reversion фандинга: {float(funding_mr):.0%}")

    if micro_lines:
        lines.append("МИКРОСТРУКТУРА")
        lines.extend(micro_lines)
        lines.append("")

    lines.append("ВОЗМОЖНОСТИ")
    if not report.scoring_enabled:
        lines.append("Оценка торговых возможностей отключена из-за неполных данных.")
    elif report.opportunities:
        shown = report.opportunities[:top_n]
        lines.append("Лучшие идеи: " + ", ".join(
            f"{x.symbol} ({x.score:.0f}/100, {_bias_ru(x.directional_bias)}, уверенность {x.confidence:.0%})" for x in shown
        ))
    else:
        lines.append("Подходящих торговых возможностей сейчас не выявлено.")
    lines.append("")

    lines.append("РИСК")
    pr = report.portfolio_risk
    lines.append(f"Множитель риска: {pr.risk_multiplier:.2f}")
    lines.append(f"Режим портфеля: {'защитный' if pr.defensive_mode else 'рабочий'}")
    lines.append(f"Агрессивный режим: {'включен' if pr.aggressive_mode_enabled else 'выключен'}")
    if pr.defensive_mode or pr.recommended_exposure_cap_pct < 100.0:
        lines.append(f"Рекомендуемая загрузка капитала: до {pr.recommended_exposure_cap_pct:.0f}%")
    lines.append("")

    lines.append("КРАТКИЙ ВЫВОД")
    lines.append(
        f"Рынок находится в режиме '{_regime_ru(report.global_regime.regime)}', "
        f"текущая активность оценивается как {volatility_brief}."
    )
    if report.data_health_status != DataHealthStatus.OK:
        lines.append("Часть данных недоступна, поэтому надежность сигналов ниже обычного.")
        lines.append("Сейчас приоритет - осторожный режим и ограниченная загрузка капитала.")
    else:
        lines.append("Данные полные, но входы стоит фильтровать по контексту тренда и риска.")

    notes = _contextual_notes(payload)
    if notes:
        lines.append("")
        for note in notes:
            lines.append(note)

    return "\n".join(lines)


def _contextual_notes(payload: dict) -> List[str]:
    """Generate contextual observations based on payload data."""
    notes: List[str] = []
    global_block = payload.get("global", {})
    metrics = global_block.get("metrics", {})
    regime_name = global_block.get("regime", {}).get("name", "")
    local_ctx = payload.get("local", {}).get("context", {})

    funding_pct = metrics.get("funding_pct")
    oi_delta_pct = metrics.get("oi_delta_pct")
    vol_regime = metrics.get("volatility_regime")
    adx_val = local_ctx.get("adx")

    if funding_pct is not None and float(funding_pct) > 0.05 and regime_name == "trend_up":
        notes.append("Повышенный фандинг при восходящем тренде — риск коррекции при раскрутке позиций.")
    if funding_pct is not None and float(funding_pct) < -0.05 and regime_name == "trend_down":
        notes.append("Отрицательный фандинг при нисходящем тренде — возможен short squeeze.")
    if vol_regime == "low" and adx_val is not None and float(adx_val) < 20:
        notes.append("Низкая волатильность и слабый тренд — рынок в сжатии, вероятен импульсный выход.")
    if regime_name == "panic" and oi_delta_pct is not None and float(oi_delta_pct) < -5:
        notes.append("Массовое закрытие позиций — возможна капитуляция.")

    conv_scores = payload.get("convergence_scores", {})
    btc_sym = global_block.get("symbol", "BTCUSDT")
    conv = conv_scores.get(btc_sym, 1.0)
    if conv > 1.10:
        notes.append("Сильная конвергенция таймфреймов — повышенная надёжность сигналов.")
    elif conv < 0.80:
        notes.append("Расхождение таймфреймов — сигналы менее надёжны, осторожность.")

    # Orderbook divergence from price trend
    ob_imb = (payload.get("features", {}).get(btc_sym, {}) or {}).get("orderbook_imbalance")
    if ob_imb is not None and regime_name == "trend_up" and float(ob_imb) < -0.25:
        notes.append("Стакан расходится с трендом: давление продавцов при восходящем движении.")
    if ob_imb is not None and regime_name == "trend_down" and float(ob_imb) > 0.25:
        notes.append("Стакан расходится с трендом: давление покупателей при нисходящем движении.")

    # Basis acceleration warning
    basis_acc = (payload.get("features", {}).get(btc_sym, {}) or {}).get("basis_acceleration")
    if basis_acc is not None and abs(float(basis_acc)) > 0.5:
        if float(basis_acc) > 0:
            notes.append("Ускоренный рост базиса — возможно нарастание спекулятивных позиций.")
        else:
            notes.append("Резкое сжатие базиса — возможна ликвидация или деливеридж.")

    return notes


def format_json_report(report: MarketIntelligenceReport) -> str:
    return json.dumps(report.payload, ensure_ascii=False, separators=(",", ":"))