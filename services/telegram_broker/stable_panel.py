from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import requests

from services.telegram_broker import bot as base


ORIGINAL_ROUTE = base.route
ORIGINAL_CONFIRM = base._confirm_action
ORIGINAL_HANDLE_MESSAGE = base.handle_message
ORIGINAL_SEND = base.send

DB = Path(
    os.getenv(
        "SIDECAR_EXECUTION_DB",
        str(base.SIDECAR_RUNTIME / "execution_state.sqlite"),
    )
)

TESTNET = "https://testnet.binance.vision"


BUTTON_ACTIONS = {
    "📊 Status": "status",
    "💰 Balance": "balance",
    "📋 Open Trades": "x_open",
    "📜 History": "x_history",
    "📡 Current Signal": "x_current",
    "🕘 Last Signal": "last_signal",
    "🗂 Recent Signals": "x_recent",
    "💵 P&L 24h": "x_pnl24",
    "₿ BTC Pair": "pair",
    "🔄 Choose Pair": "pairs",
    "🌊 Money Flow": "flow",
    "🩺 Health": "x_health",
    "📊 CoinMarketCap": "x_cmc",
    "🦎 CoinGecko": "x_cg",
    "🟢 Buy 1 BTC": "x_manual_buy",
    "🔴 Sell 1 BTC": "x_manual_sell",
    "▶️ Resume Entries": "entries_on",
    "⏸ Pause Entries": "x_pause",
    "🔁 Reconcile": "x_reconcile",
    "🛡 Protection": "x_protection",
    "⚙️ System": "x_system",
    "🧯 Recover Stream": "x_recover",
    "🚨 Emergency": "menu_emergency",
    "📄 Logs": "logs",
    "ℹ️ Help": "help",
}

FIXED_KEYBOARD = [
    ["📊 Status", "💰 Balance"],
    ["📋 Open Trades", "📜 History"],
    ["📡 Current Signal", "🕘 Last Signal"],
    ["🗂 Recent Signals", "💵 P&L 24h"],
    ["₿ BTC Pair", "🔄 Choose Pair"],
    ["🌊 Money Flow", "🩺 Health"],
    ["📊 CoinMarketCap", "🦎 CoinGecko"],
    ["🟢 Buy 1 BTC", "🔴 Sell 1 BTC"],
    ["▶️ Resume Entries", "⏸ Pause Entries"],
    ["🔁 Reconcile", "🛡 Protection"],
    ["⚙️ System", "🧯 Recover Stream"],
    ["🚨 Emergency", "📄 Logs"],
    ["ℹ️ Help"],
]


LEGACY_TEXT_ACTIONS = {
    "status": "status",
    "balance": "balance",
    "open trades": "x_open",
    "history": "x_history",
    "current signal": "x_current",
    "last signal": "last_signal",
    "recent signals": "x_recent",
    "p and l 24h": "x_pnl24",
    "pnl 24h": "x_pnl24",
    "profit": "x_pnl24",
    "btc pair": "pair",
    "choose pair": "pairs",
    "money flow": "flow",
    "health": "x_health",
    "coinmarketcap": "x_cmc",
    "coin gecko": "x_cg",
    "coingecko": "x_cg",
    "buy 1 btc": "x_manual_buy",
    "sell 1 btc": "x_manual_sell",
    "resume entries": "entries_on",
    "pause entries": "x_pause",
    "reconcile": "x_reconcile",
    "protection": "x_protection",
    "system": "x_system",
    "recover stream": "x_recover",
    "emergency": "menu_emergency",
    "logs": "logs",
    "help": "help",
}


def normalize_button_text(text):
    value = str(text or "").casefold().replace("₿", " btc ")
    value = value.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def resolve_button_action(text):
    return BUTTON_ACTIONS.get(str(text or "").strip()) or LEGACY_TEXT_ACTIONS.get(
        normalize_button_text(text)
    )


def fixed_keyboard_markup():
    return {
        "keyboard": [
            [{"text": label} for label in row]
            for row in FIXED_KEYBOARD
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "Bitcoin TestNet controls",
    }


def send_fixed(text, chat_id=None, buttons=None):
    # Confirmation/pair-selection actions keep their existing inline keyboard.
    # Normal responses carry the persistent owner keyboard below the chat.
    if buttons:
        return ORIGINAL_SEND(text, chat_id, buttons)
    if not base.TOKEN:
        raise RuntimeError("Telegram token is unavailable; notification not acknowledged")
    response = requests.post(
        base.BASE + "/sendMessage",
        data={
            "chat_id": chat_id or base.OWNER,
            "text": base.redact_text(str(text))[:4000],
            "reply_markup": json.dumps(fixed_keyboard_markup()),
        },
        timeout=15,
    )
    response.raise_for_status()
    if response.json().get("ok") is not True:
        raise RuntimeError("Telegram did not acknowledge the notification")


def safe(v, default="—"):
    return default if v in (None, "") else str(v)


def dec(v):
    try:
        x = Decimal(str(v))
        return x if x.is_finite() else None
    except Exception:
        return None


def money(v):
    x = dec(v)
    return "—" if x is None else f"{x:,.2f}"


def db():
    if not DB.exists():
        raise RuntimeError(
            "execution database is missing at " + str(DB)
        )

    con = sqlite3.connect(
        f"file:{DB}?mode=ro",
        uri=True,
        timeout=5,
    )

    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=3000")

    check = con.execute(
        "PRAGMA quick_check"
    ).fetchone()

    if not check or str(check[0]).lower() != "ok":
        con.close()
        raise RuntimeError(
            "execution database integrity check failed"
        )

    return con


def sidecar(command, args=None):
    result = base.sidecar_command(
        command,
        args or {},
        wait=True,
    )

    if result.get("ok") is not True:
        return None, str(
            result.get(
                "result",
                "sidecar command failed",
            )
        )

    raw = result.get("result")

    if isinstance(raw, dict):
        return raw, None

    try:
        parsed = json.loads(str(raw))
    except Exception:
        return {"text": str(raw)}, None

    if isinstance(parsed, dict):
        return parsed, None

    return {"value": parsed}, None


def active_trades(con):
    rows = con.execute(
        "SELECT * FROM trade_records "
        "WHERE lifecycle_state NOT IN "
        "('EXIT_FILLED','ENTRY_REJECTED') "
        "ORDER BY updated_at DESC"
    ).fetchall()

    return [dict(x) for x in rows]


def latest_intent(con, trade_id):
    rows = con.execute(
        "SELECT operation,endpoint,request_json,state "
        "FROM operation_intents "
        "WHERE trade_id=? "
        "ORDER BY updated_at DESC",
        (str(trade_id),),
    ).fetchall()

    wanted = {
        "workingPrice",
        "pendingAbovePrice",
        "abovePrice",
        "pendingBelowStopPrice",
        "belowStopPrice",
        "pendingBelowPrice",
        "belowPrice",
        "pendingPrice",
        "price",
    }

    for row in rows:
        try:
            request = json.loads(row["request_json"])
        except Exception:
            continue

        if not isinstance(request, dict):
            continue

        if not wanted.intersection(request):
            continue

        return {
            "operation": row["operation"],
            "endpoint": row["endpoint"],
            "state": row["state"],
            "request": request,
        }

    return None


def protection_lines(intent):
    if not intent:
        return []

    r = intent["request"]

    mapping = (
        ("workingPrice", "Requested buy"),
        ("pendingAbovePrice", "Take profit"),
        ("abovePrice", "Take profit"),
        ("pendingBelowStopPrice", "Stop trigger"),
        ("belowStopPrice", "Stop trigger"),
        ("pendingBelowPrice", "Stop/trailing limit"),
        ("belowPrice", "Stop/trailing limit"),
        ("pendingPrice", "Trailing limit"),
        ("pendingBelowTrailingDelta", "Trailing delta bips"),
        ("belowTrailingDelta", "Trailing delta bips"),
        ("pendingTrailingDelta", "Trailing delta bips"),
        ("trailingDelta", "Trailing delta bips"),
    )

    output = []
    used = set()

    for key, label in mapping:
        if (
            key in r
            and r[key] not in (None, "")
            and label not in used
        ):
            output.append(
                f"{label}: {r[key]}"
            )
            used.add(label)

    if intent.get("endpoint"):
        output.append(
            "Order type: " + str(intent["endpoint"])
        )

    return output


def exit_result(con, trade):
    identifiers = {int(trade[k]) for k in ("take_profit_order_id", "stop_order_id")
                   if trade.get(k) not in (None, "", 0, "0", -1, "-1")}
    for item in con.execute(
            "SELECT exchange_order_id FROM operation_intents WHERE trade_id=? "
            "AND operation='EMERGENCY_EXIT' AND state='CONFIRMED'", (trade["trade_id"],)):
        if item[0]: identifiers.add(int(item[0]))
    if not identifiers: return None
    placeholders = ','.join('?' for _ in identifiers)
    rows = con.execute("SELECT payload_json FROM exchange_events WHERE order_id IN ("+
                       placeholders+") ORDER BY id", tuple(sorted(identifiers)))
    fills = {}
    for row in rows:
        try:
            event=json.loads(row[0])
            if event.get('S', event.get('side')) != 'SELL': continue
            qty=dec(event.get('z', event.get('executedQty')))
            proceeds=dec(event.get('Z', event.get('cummulativeQuoteQty')))
            if qty is None or qty <= 0 or proceeds is None or proceeds < 0: continue
            oid=str(event.get('i',event.get('orderId')))
            stamp=float(event.get('E',event.get('eventTime',0)))
            if stamp > 10000000000: stamp /= 1000
            if oid not in fills or qty >= fills[oid][0]: fills[oid]=(qty,proceeds,stamp)
        except (ValueError, TypeError): continue
    entry=dec(trade.get('average_entry_price'))
    expected=dec(trade.get('filled_quantity'))
    if not fills or entry is None or expected is None: return None
    qty=sum((x[0] for x in fills.values()), Decimal(0))
    proceeds=sum((x[1] for x in fills.values()), Decimal(0))
    if qty > expected: return None
    return {'pnl':proceeds-entry*qty,'exit_qty':qty,'exit_quote':proceeds,
            'quote_asset':trade['pair'].split('/')[1],
            'closed':max(x[2] for x in fills.values()),'fee_basis':'gross; fees excluded'}



def status_text():
    s, error = sidecar("status")

    h = base.read_json(
        base.SIDECAR_RUNTIME /
        "sidecar_health.json",
        {},
    ) or {}

    if s is None:
        return (
            "📊 STATUS\n\n"
            "Unavailable: " +
            str(error)
        )

    active_count = "?"
    risk_pause = "unknown"

    try:
        con = db()

        active_count = len(
            active_trades(con)
        )

        row = con.execute(
            "SELECT payload_json "
            "FROM risk_guard_state "
            "WHERE singleton=1"
        ).fetchone()

        if row:
            risk = json.loads(row[0])
            risk_pause = (
                risk.get("global_pause")
                or "none"
            )

        con.close()

    except Exception:
        pass

    active = s.get("active_pair")

    if not isinstance(active, dict):
        active = {}

    return (
        "📊 BITCOIN TESTNET STATUS\n\n"
        f"Mode: {safe(s.get('mode')).upper()}\n"
        f"Started: {'YES' if s.get('started') else 'NO'}\n"
        f"Entries armed: "
        f"{'YES' if s.get('entries_armed') else 'NO'}\n"
        f"Pair: {safe(active.get('pair'), 'BTC/USDT')}\n"
        f"Open trades: {active_count}\n"
        f"Unresolved intents: "
        f"{safe(s.get('unresolved_intents'), '0')}\n"
        f"Reconciliation: "
        f"{safe(h.get('last_reconciliation_status'))}\n"
        f"User stream: "
        f"{'OK' if h.get('user_stream_ok') else 'NOT READY'}\n"
        f"Freqtrade pair: "
        f"{'OK' if h.get('freqtrade_pair_ready') else 'NOT READY'}\n"
        f"Pause reason: "
        f"{safe(h.get('pause_reason'), 'none')}\n"
        f"Risk pause: {risk_pause}"
    )


def balance_text():
    b, error = sidecar("balance")

    if b is None:
        return (
            "💰 BALANCE\n\n"
            "Unavailable: " +
            str(error)
        )

    return (
        "💰 BINANCE SPOT TESTNET BALANCE\n\n"
        f"Pair: {safe(b.get('pair'))}\n"
        f"BTC free: {safe(b.get('BTC_free'))}\n"
        f"{safe(b.get('quote_asset'))} free: {money(b.get('quote_free'))}\n\n"
        "Virtual TestNet funds only."
    )


def open_trades_text():
    try:
        con = db()
        trades = active_trades(con)

        if not trades:
            con.close()

            return (
                "📋 OPEN TRADES\n\n"
                "No open trade.\n\n"
                "The bot will wait for a genuine "
                "IctSmcStrategy entry signal."
            )

        lines = [
            f"📋 OPEN TRADES ({len(trades)})",
            "",
        ]

        for t in trades[:5]:
            intent = latest_intent(
                con,
                t["trade_id"],
            )

            lines += [
                f"Trade: {t['trade_id']}",
                f"Pair: {t['pair']}",
                f"State: {t['lifecycle_state']}",
                f"Entry price: "
                f"{safe(t['average_entry_price'])}",
                f"Filled BTC: "
                f"{safe(t['filled_quantity'])}",
                f"Protected BTC: "
                f"{safe(t['protected_quantity'])}",
                f"Protection: "
                f"{safe(t['protection_mode'])}",
            ]

            lines += protection_lines(
                intent
            )

            lines += [
                f"Reconciliation: "
                f"{safe(t['reconciliation_status'])}",
                "",
            ]

        con.close()

        return "\n".join(lines)[:3900]

    except Exception as exc:
        return (
            "📋 OPEN TRADES\n\n"
            "Unavailable: " +
            base._safe_error(exc)
        )


def history_text():
    try:
        con = db()

        rows = con.execute(
            "SELECT * FROM trade_records "
            "ORDER BY updated_at DESC "
            "LIMIT 10"
        ).fetchall()

        if not rows:
            con.close()

            return (
                "📜 HISTORY\n\n"
                "No durable trades recorded."
            )

        lines = [
            "📜 TRADE HISTORY",
            "",
        ]

        for raw in rows:
            t = dict(raw)

            p = (
                exit_result(con, t)
                if
                t["lifecycle_state"]
                == "EXIT_FILLED"
                else None
            )

            suffix = ""

            if p:
                suffix = (
                    f" | gross {p['pnl']:+.2f} {p['quote_asset']} (fees excluded)"
                )

            lines.append(
                f"{t['updated_at'][:19]} | "
                f"{t['pair']} | "
                f"{t['lifecycle_state']}"
                f"{suffix}"
            )

            lines.append(
                "  Entry "
                f"{safe(t['average_entry_price'])}"
                " | Qty "
                f"{safe(t['filled_quantity'])}"
            )

        con.close()

        return "\n".join(lines)[:3900]

    except Exception as exc:
        return (
            "📜 HISTORY\n\n"
            "Unavailable: " +
            base._safe_error(exc)
        )


def pnl24_text():
    cutoff=time.time()-86400
    con=None
    try:
        con=db()
        totals={}
        verified=0
        missing=0
        rows=con.execute("SELECT * FROM trade_records WHERE lifecycle_state='EXIT_FILLED'")
        for raw in rows:
            result=exit_result(con,dict(raw))
            if result is None:
                missing+=1
                continue
            if result['closed'] < cutoff: continue
            quote_asset=result['quote_asset']
            totals[quote_asset]=totals.get(quote_asset,Decimal(0))+result['pnl']
            verified+=1
        lines=['Gross realised P&L — last 24 hours','',f'Closed trades with event evidence: {verified}']
        lines.extend(f'{asset}: {value:+.2f}' for asset,value in sorted(totals.items()))
        if not totals: lines.append('No closed-trade proceeds in this period.')
        lines += ['', 'Fees excluded; this is not net profit.',
                  f'Historical closed trades with incomplete event evidence: {missing}']
        return '\n'.join(lines)
    except Exception as exc:
        return 'P&L unavailable: '+base._safe_error(exc)
    finally:
        if con is not None: con.close()



def recent_signals_text():
    try:
        con = db()

        rows = con.execute(
            "SELECT signal_id,pair,"
            "candle_time,result,processed_at "
            "FROM processed_signals "
            "ORDER BY processed_at DESC "
            "LIMIT 10"
        ).fetchall()

        lines = [
            "🗂 RECENT SIGNALS",
            "",
        ]

        for r in rows:
            result = str(r["result"])

            if result == "accepted":
                text = "ACCEPTED"

            elif result.startswith(
                "rejected:"
            ):
                text = (
                    "REJECTED — " +
                    result.split(
                        ":",
                        1,
                    )[1]
                )

            else:
                text = result.upper()

            lines.append(
                f"{str(r['processed_at'])[:19]}"
                f" | {r['pair']} | {text}"
            )

        con.close()

        if len(lines) == 2:
            lines.append(
                "No durable signals."
            )

        return "\n".join(lines)[:3900]

    except Exception as exc:
        return (
            "🗂 RECENT SIGNALS\n\n"
            "Unavailable: " +
            base._safe_error(exc)
        )


def last_signal_text():
    try:
        con = db()

        r = con.execute(
            "SELECT signal_id,pair,"
            "candle_time,result,processed_at "
            "FROM processed_signals "
            "ORDER BY processed_at DESC "
            "LIMIT 1"
        ).fetchone()

        con.close()

        if not r:
            return (
                "🕘 LAST SIGNAL\n\n"
                "No signal recorded."
            )

        return (
            "🕘 LAST SIGNAL\n\n"
            f"Signal ID: {r['signal_id']}\n"
            f"Pair: {r['pair']}\n"
            f"Candle: {safe(r['candle_time'])}\n"
            f"Decision: {r['result']}\n"
            f"Processed: {r['processed_at']}"
        )

    except Exception as exc:
        return (
            "🕘 LAST SIGNAL\n\n"
            "Unavailable: " +
            base._safe_error(exc)
        )


def book(symbol):
    r = requests.get(
        TESTNET +
        "/api/v3/ticker/bookTicker",
        params={
            "symbol": symbol,
        },
        timeout=8,
    )

    r.raise_for_status()

    return r.json()


def current_candle(pair):
    endpoint = (
        "/pair_candles?pair=" +
        quote(pair, safe="") +
        "&timeframe=1m&limit=2"
    )

    result = base.ft_call(
        "GET",
        endpoint,
    )

    if result.get("ok") is not True:
        return {}, str(
            result.get(
                "error",
                "pair_candles unavailable",
            )
        )

    payload = result.get("data") or {}

    if not isinstance(
        payload,
        dict,
    ):
        return {}, (
            "unexpected Freqtrade response"
        )

    rows = payload.get("data") or []

    if not rows:
        return {}, (
            "no current candle"
        )

    row = rows[-1]

    if isinstance(row, dict):
        return row, None

    columns = (
        payload.get("columns")
        or []
    )

    if (
        isinstance(row, list)
        and len(row)
        == len(columns)
    ):
        return dict(
            zip(columns, row)
        ), None

    return {}, (
        "unknown candle format"
    )


def current_signal_text():
    active = (
        base.read_json(
            base.ACTIVE_PAIR_FILE,
            {},
        )
        or {}
    )

    pair = str(
        active.get("pair")
        or "BTC/USDT"
    )

    symbol = str(
        active.get("symbol")
        or pair.replace("/", "")
    )

    row, detail = current_candle(
        pair
    )

    try:
        ticker = book(symbol)
    except Exception as exc:
        ticker = {
            "bidPrice": "unavailable",
            "askPrice": "unavailable",
        }

        detail = (
            detail
            or base._safe_error(exc)
        )

    has_flag = (
        "enter_long" in row
    )

    raw_flag = row.get(
        "enter_long"
    )

    buy = (
        raw_flag is True
        or raw_flag == 1
        or str(raw_flag).lower()
        in {"1", "true"}
    )

    signal = (
        "YES"
        if buy
        else (
            "NO"
            if has_flag
            else "NO ACTIVE FLAG"
        )
    )

    lines = [
        "📡 CURRENT SIGNAL",
        "",
        f"Pair: {pair}",
        f"BUY signal now: {signal}",
        f"Candle: "
        f"{safe(row.get('date') or row.get('timestamp'))}",
        f"Latest close: "
        f"{safe(row.get('close'))}",
        f"TestNet best bid: "
        f"{safe(ticker.get('bidPrice'))}",
        f"TestNet best ask: "
        f"{safe(ticker.get('askPrice'))}",
    ]

    def number(key):
        return dec(row.get(key))

    def flag(key):
        value = row.get(key)
        return (
            value is True
            or value == 1
            or str(value).lower() in {"1", "true"}
        )

    close = number("close")
    ema9_5m = number("ema9_5m")
    ema21_5m = number("ema21_5m")
    ema50_5m = number("ema50_5m")
    macdhist_5m = number("macdhist_5m")
    vwap = number("vwap")
    ema9 = number("ema9")
    rsi = number("rsi")
    rvol = number("rvol")
    adx = number("adx")
    volume = number("volume")

    gates = [
        ("5m EMA9 > EMA21", ema9_5m is not None and ema21_5m is not None and ema9_5m > ema21_5m, f"{safe(row.get('ema9_5m'))} > {safe(row.get('ema21_5m'))}"),
        ("5m EMA21 > EMA50", ema21_5m is not None and ema50_5m is not None and ema21_5m > ema50_5m, f"{safe(row.get('ema21_5m'))} > {safe(row.get('ema50_5m'))}"),
        ("Close > 5m EMA50", close is not None and ema50_5m is not None and close > ema50_5m, f"{safe(row.get('close'))} > {safe(row.get('ema50_5m'))}"),
        ("5m MACD histogram > 0", macdhist_5m is not None and macdhist_5m > 0, safe(row.get("macdhist_5m"))),
        ("Close > VWAP", close is not None and vwap is not None and close > vwap, f"{safe(row.get('close'))} > {safe(row.get('vwap'))}"),
        ("EMA9/21 pullback in last 3 bars", flag("pullback"), safe(row.get("pullback"))),
        ("Close > 1m EMA9", close is not None and ema9 is not None and close > ema9, f"{safe(row.get('close'))} > {safe(row.get('ema9'))}"),
        ("1m EMA9 rising", flag("ema9_rising"), safe(row.get("ema9_rising"))),
        ("RSI > 50", rsi is not None and rsi > 50, safe(row.get("rsi"))),
        ("RSI rising", flag("rsi_rising"), safe(row.get("rsi_rising"))),
        ("RVOL >= 1.5", rvol is not None and rvol >= Decimal("1.5"), safe(row.get("rvol"))),
        ("ADX > 20", adx is not None and adx > 20, safe(row.get("adx"))),
        ("Volume > 0", volume is not None and volume > 0, safe(row.get("volume"))),
    ]

    lines += ["", "Protected entry gates:"]
    for label, passed, value in gates:
        lines.append(("PASS" if passed else "FAIL") + f" — {label}: {value}")

    if not buy:
        lines += [
            "",
            "No new buy order should be "
            "created without a genuine "
            "IctSmcStrategy entry signal.",
        ]

    else:
        lines += [
            "",
            "A genuine BUY flag is present.",
            "The execution sidecar must still "
            "pass balance, exposure, Binance "
            "filters and protection checks.",
        ]

    if detail:
        lines += [
            "",
            "Indicator detail: " +
            str(detail),
        ]

    try:
        con = db()
        trades = active_trades(con)

        if trades:
            t = trades[0]

            lines += [
                "",
                "CURRENT OPEN TRADE",
                f"Entry average: "
                f"{safe(t['average_entry_price'])}",
                f"State: "
                f"{safe(t['lifecycle_state'])}",
            ]

            lines += protection_lines(
                latest_intent(
                    con,
                    t["trade_id"],
                )
            )

        con.close()

    except Exception as exc:
        lines += [
            "",
            "Open-trade detail unavailable: " +
            base._safe_error(exc),
        ]

    return "\n".join(lines)[:3900]


def pair_text():
    p, error = sidecar("pair")

    if p is None:
        return (
            "₿ BTC PAIR\n\n"
            "Unavailable: " +
            str(error)
        )

    active = (
        p.get("active")
        if isinstance(
            p.get("active"),
            dict,
        )
        else {}
    )

    switch = (
        p.get("switch")
        if isinstance(
            p.get("switch"),
            dict,
        )
        else {}
    )

    symbol = str(
        active.get("symbol")
        or "BTCUSDT"
    )

    exchange_status = "unknown"

    try:
        r = requests.get(
            TESTNET +
            "/api/v3/exchangeInfo",
            params={
                "symbol": symbol,
            },
            timeout=8,
        )

        r.raise_for_status()

        symbols = (
            r.json().get(
                "symbols"
            )
            or []
        )

        if symbols:
            exchange_status = (
                symbols[0].get(
                    "status",
                    "unknown",
                )
            )

    except Exception:
        pass

    return (
        "₿ ACTIVE BTC PAIR\n\n"
        f"Pair: {safe(active.get('pair'))}\n"
        f"Symbol: {symbol}\n"
        f"Binance TestNet: {exchange_status}\n"
        f"Generation: "
        f"{safe(active.get('generation'))}\n"
        f"Pair switch: "
        f"{safe(switch.get('stage') or switch.get('status'), 'ACTIVE')}\n\n"
        "Choose Pair retains the bot's "
        "existing confirmation, flat-state "
        "and funding checks."
    )


def flow_text():
    try:
        f = base._flow_summary()

        classification = (
            f.get("classification")
            if isinstance(
                f.get("classification"),
                dict,
            )
            else {}
        )

        spot = (
            f.get("spot")
            if isinstance(
                f.get("spot"),
                dict,
            )
            else {}
        )

        lines = [
            "🌊 MONEY FLOW",
            "",
            f"Pair: {safe(f.get('pair'))}",
            f"Snapshot: "
            f"{'FRESH' if f.get('fresh') else 'STALE/UNAVAILABLE'}",
            f"Flow OK: "
            f"{'YES' if f.get('ok') else 'NO'}",
            f"Decision: "
            f"{safe(classification.get('decision'))}",
            f"Bullish: "
            f"{'YES' if classification.get('bullish') else 'NO'}",
        ]

        keys = (
            "taker_buy_ratio",
            "spot_imbalance",
            "orderbook_imbalance",
            "depth_imbalance",
            "price",
            "last_price",
        )

        for key in keys:
            if (
                key in spot
                and spot[key]
                not in (None, "")
            ):
                lines.append(
                    key.replace(
                        "_",
                        " ",
                    ).title()
                    + ": "
                    + str(spot[key])
                )

        lines += [
            "",
            "Hard flow gate: " +
            os.getenv(
                "REQUIRE_FLOW_CONTEXT",
                "false",
            ),
        ]

        return "\n".join(
            lines
        )[:3900]

    except Exception as exc:
        return (
            "🌊 MONEY FLOW\n\n"
            "Unavailable: " +
            base._safe_error(exc)
        )


def external_provider_text(provider):
    names = {
        "coinmarketcap": "📊 COINMARKETCAP",
        "coingecko": "🦎 COINGECKO",
    }
    snapshot = base.read_json(base.MONEYFLOW_FILE, {}) or {}
    external = snapshot.get("external_context") if isinstance(snapshot, dict) else {}
    if not isinstance(external, dict):
        external = {}
    providers = external.get("providers") or {}
    row = providers.get(provider) if isinstance(providers, dict) else None
    title = names.get(provider, provider.upper())
    if not isinstance(row, dict):
        return title + "\n\nProvider data is not available in the Moneyflow snapshot."
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    quota = row.get("quota") if isinstance(row.get("quota"), dict) else {}
    identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
    change = dec(data.get("percent_change_24h"))
    change_text = "—" if change is None else f"{change:+.3f}%"
    lines = [
        title,
        "",
        f"Asset: {safe(identity.get('name'), 'Bitcoin')} ({safe(identity.get('symbol'), 'BTC')})",
        f"Status: {safe(row.get('status')).upper()}",
        f"Available: {'YES' if row.get('available') else 'NO'}",
        f"Fresh: {'YES' if row.get('fresh') else 'NO'}",
        f"BTC price: ${money(data.get('price_usd'))}",
        f"24h change: {change_text}",
        f"Market cap: ${money(data.get('market_cap_usd'))}",
        f"24h volume: ${money(data.get('volume_24h_usd'))}",
        f"Cache age: {safe(row.get('cache_age_seconds'))} s",
        "",
        f"Monthly local attempts: {safe(quota.get('monthly_attempts_reserved'), '0')} / {safe(quota.get('monthly_attempt_cap'))}",
        "Advisory only: YES",
        f"Affects entry decision: {'YES' if external.get('affects_entry_decision') else 'NO'}",
    ]
    return "\n".join(lines)[:3900]


SIGNAL_NOTIFY_DB = Path("/app/shared/runtime/telegram/signal_notifications.sqlite3")
SIGNAL_PROCESSED_DIR = Path(os.getenv("SIGNAL_PROCESSED", "/app/shared/signals/processed"))
SIGNAL_REJECTED_DIR = Path(os.getenv("SIGNAL_REJECTED", "/app/shared/signals/rejected"))



def _signal_payload(signal_id):
    for directory in (SIGNAL_PROCESSED_DIR, SIGNAL_REJECTED_DIR):
        path = directory / f"{signal_id}.json"
        try:
            payload = base.read_json(path, None)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return payload
    return {}


def _signal_notification_text(row):
    signal_id = str(row["signal_id"])
    envelope = _signal_payload(signal_id)
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    metrics = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    strategy = str(payload.get("strategy") or "IctSmcStrategy")
    pair = str(row["pair"] or payload.get("pair") or "BTC/USDT")
    result = str(row["result"] or "unknown")
    accepted = result == "accepted"
    lines = [
        "📡 AUTOMATIC STRATEGY SIGNAL",
        "",
        f"Strategy: {strategy}",
        f"Pair: {pair}",
        f"Candle: {safe(row['candle_time'])}",
        f"Signal ID: {signal_id}",
        f"Entry tag: {safe(payload.get('entry_tag'))}",
        f"Execution result: {'ACCEPTED' if accepted else 'REJECTED'}",
    ]
    if not accepted:
        reason = result.split("rejected:", 1)[1] if result.startswith("rejected:") else result
        lines.append("Reason: " + reason)
    for label, key in (("Close", "close"), ("RSI", "rsi"), ("RVOL", "rvol"), ("ADX", "adx"), ("5m MACD hist", "macdhist_5m")):
        if metrics.get(key) not in (None, ""):
            lines.append(f"{label}: {metrics.get(key)}")
    if accepted:
        try:
            con = db()
            trade = con.execute(
                "SELECT lifecycle_state,average_entry_price,filled_quantity,protected_quantity "
                "FROM trade_records WHERE trade_id=?", (signal_id,),
            ).fetchone()
            con.close()
            if trade:
                lines += [
                    "",
                    f"Trade state: {safe(trade['lifecycle_state'])}",
                    f"Entry price: {safe(trade['average_entry_price'])}",
                    f"Filled BTC: {safe(trade['filled_quantity'])}",
                    f"Protected BTC: {safe(trade['protected_quantity'])}",
                ]
        except Exception:
            pass
    lines += ["", "This notification is read-only; it does not create or alter a strategy signal."]
    return "\n".join(lines)[:3900]


def _notifier_db():
    SIGNAL_NOTIFY_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SIGNAL_NOTIFY_DB, timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS notified_signals (signal_id TEXT PRIMARY KEY, notified_at REAL NOT NULL, result TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS notifier_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.commit()
    return con


def signal_notifier_loop():
    while True:
        notify_db = None
        side = None
        try:
            notify_db = _notifier_db()
            boot = notify_db.execute("SELECT value FROM notifier_meta WHERE key='bootstrapped'").fetchone()
            side = db()
            if not boot:
                for row in side.execute("SELECT signal_id,result FROM processed_signals"):
                    notify_db.execute(
                        "INSERT OR IGNORE INTO notified_signals(signal_id,notified_at,result) VALUES(?,?,?)",
                        (str(row["signal_id"]), 0.0, str(row["result"])),
                    )
                notify_db.execute("INSERT OR REPLACE INTO notifier_meta(key,value) VALUES('bootstrapped',?)", (str(time.time()),))
                notify_db.commit()
            rows = side.execute(
                "SELECT signal_id,pair,candle_time,result,processed_at FROM processed_signals "
                "WHERE (processed_at,signal_id) > (?,?) ORDER BY processed_at,signal_id LIMIT 200",
                tuple(json.loads((notify_db.execute(
                    "SELECT value FROM notifier_meta WHERE key='cursor'"
                ).fetchone() or ['["",""]'])[0]))
            ).fetchall()
            for row in rows:
                exists = notify_db.execute(
                    "SELECT 1 FROM notified_signals WHERE signal_id=?", (str(row["signal_id"]),)
                ).fetchone()
                cursor = json.dumps([str(row["processed_at"]), str(row["signal_id"])])
                if exists:
                    notify_db.execute("INSERT OR REPLACE INTO notifier_meta(key,value) VALUES('cursor',?)", (cursor,))
                    notify_db.commit()
                    continue
                text = _signal_notification_text(row)
                send_fixed(text, base.OWNER)
                notify_db.execute(
                    "INSERT OR REPLACE INTO notified_signals(signal_id,notified_at,result) VALUES(?,?,?)",
                    (str(row["signal_id"]), time.time(), str(row["result"])),
                )
                notify_db.execute("INSERT OR REPLACE INTO notifier_meta(key,value) VALUES('cursor',?)", (cursor,))
                notify_db.commit()
                base.audit(
                    "telegram_signal_notification_sent", actor="telegram-broker",
                    details={"signal_id": str(row["signal_id"]), "result": str(row["result"])},
                )
        except Exception as exc:
            try:
                base.audit(
                    "telegram_signal_notifier_error", severity="WARNING", actor="telegram-broker",
                    details={"error": base._safe_error(exc)},
                )
            except Exception:
                pass
        finally:
            if side is not None:
                try: side.close()
                except Exception: pass
            if notify_db is not None:
                try: notify_db.close()
                except Exception: pass
        time.sleep(3)


def start_signal_notifier():
    thread = threading.Thread(target=signal_notifier_loop, name="telegram-signal-notifier", daemon=True)
    thread.start()
    return thread


def health_text():
    h=base.read_json(base.SIDECAR_RUNTIME/'sidecar_health.json',{}) or {}
    try: fresh=-30 <= time.time()-float(h.get('ts',0)) < 30
    except (TypeError,ValueError): fresh=False
    ft=base.ft_call('GET','/health')
    ready=lambda key: 'OK' if fresh and h.get(key) is True else 'NOT READY'
    return '\n'.join(['Health','',f'Sidecar: {ready("ok")}',
        f'Snapshot current: {fresh}',f'Reconciliation: {ready("reconciliation_ok")}',
        f'User stream: {ready("user_stream_ok")}',f'Moneyflow: {ready("moneyflow_ok")}',
        f'Adaptive protection enabled: {h.get("auto_protection_enabled", "unknown")}',
        f'Adaptive decision: {h.get("auto_protection_status", "unavailable")}',
        f'Freqtrade API: {"OK" if ft.get("ok") is True else "NOT READY"}'])



def protection_text():
    try:
        con = db()
        trades = active_trades(con)

        if not trades:
            con.close()

            return (
                "🛡 PROTECTION\n\n"
                "No open trade."
            )

        t = trades[0]

        lines = [
            "🛡 ACTIVE PROTECTION",
            "",
            f"Trade: {t['trade_id']}",
            f"Mode: "
            f"{safe(t['protection_mode'])}",
            f"Entry: "
            f"{safe(t['average_entry_price'])}",
            f"Protected BTC: "
            f"{safe(t['protected_quantity'])}",
            f"State: "
            f"{safe(t['lifecycle_state'])}",
        ]

        lines += protection_lines(
            latest_intent(
                con,
                t["trade_id"],
            )
        )

        con.close()
        h=base.read_json(base.SIDECAR_RUNTIME / "sidecar_health.json", {}) or {}
        lines += ["", "Adaptive enabled: " + str(h.get("auto_protection_enabled", "unknown")),
                  "Adaptive decision: " + str(h.get("auto_protection_status", "unavailable")),
                  "OCO_TRAILING already contains a native trailing leg.",
                  "The trailing trigger moves; the displayed SELL limit stays fixed."]
        return "\n".join(lines)[:3900]

    except Exception as exc:
        return (
            "🛡 PROTECTION\n\n"
            "Unavailable: " +
            base._safe_error(exc)
        )


def logs_text():
    lines = [
        "📄 RECENT BOT EVENTS",
        "",
    ]

    try:
        path = (
            base.AUDIT_DIR /
            "events.jsonl"
        )

        if path.exists():
            with path.open("rb") as handle:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 65536))
                recent = handle.read().decode("utf-8", errors="replace").splitlines()[-15:]

            for raw in recent:
                try:
                    item = json.loads(raw)

                    ts = str(
                        item.get("ts", "")
                    )[11:19]

                    event = safe(
                        item.get("event"),
                        "event",
                    )

                    severity = safe(
                        item.get(
                            "severity"
                        ),
                        "INFO",
                    )

                    details = (
                        item.get("details")
                        if isinstance(
                            item.get("details"),
                            dict,
                        )
                        else {}
                    )

                    reason = (
                        details.get("reason")
                        or details.get("message")
                        or details.get("error")
                        or ""
                    )

                    suffix = (
                        " — " + str(reason)
                        if reason
                        else ""
                    )

                    lines.append(
                        f"{ts} {severity} "
                        f"{event}{suffix}"
                    )

                except Exception:
                    continue

    except Exception as exc:
        lines.append(
            "Audit log unavailable: " +
            base._safe_error(exc)
        )

    ft = base.ft_call(
        "GET",
        "/logs?limit=8",
    )

    if ft.get("ok") is not True:
        lines += [
            "",
            "Freqtrade log API: " +
            str(
                ft.get(
                    "error",
                    "unavailable",
                )
            ),
        ]

    return "\n".join(lines)[-3900:]


def system_text():
    return (
        "⚙️ SYSTEM\n\n"
        f"Mode: "
        f"{os.getenv('EXECUTION_MODE', 'unknown').upper()}\n"
        f"Sidecar runtime: "
        f"{base.SIDECAR_RUNTIME}\n"
        f"Execution DB: {DB}\n"
        f"Flow hard gate: "
        f"{os.getenv('REQUIRE_FLOW_CONTEXT', 'false')}\n"
        f"External confluence hard gate: "
        f"{os.getenv('REQUIRE_EXTERNAL_CONFLUENCE', 'false')}\n\n"
        "This Telegram layer does not "
        "modify the trading strategy or "
        "protected execution/risk core."
    )


def help_text():
    return (
        "ℹ️ BITCOIN TESTNET PANEL\n\n"
        "Read-only: Status, Balance, "
        "Open Trades, History, Current Signal, "
        "Last Signal, Recent Signals, P&L 24h, "
        "BTC Pair, Money Flow, CoinMarketCap, CoinGecko, Health and Logs.\n\n"
        "Controls: Choose Pair, Resume Entries, "
        "Pause Entries, Reconcile, Recover Stream, "
        "Protection and Emergency.\n\n"
        "Manual TestNet: Buy 1 BTC and Sell 1 BTC "
        "require owner confirmation and use the verified sidecar path.\n\n"
        "Automatic trading still requires a genuine "
        "IctSmcStrategy signal. "
        "New genuine signals are pushed automatically to Telegram; no Telegram button creates a synthetic signal."
    )


def menu():
    return [
        [
            {
                "text": "📊 Status",
                "callback_data": "do|status",
            },
            {
                "text": "💰 Balance",
                "callback_data": "do|balance",
            },
        ],
        [
            {
                "text": "📋 Open Trades",
                "callback_data": "do|x_open",
            },
            {
                "text": "📜 History",
                "callback_data": "do|x_history",
            },
        ],
        [
            {
                "text": "📡 Current Signal",
                "callback_data": "do|x_current",
            },
            {
                "text": "🕘 Last Signal",
                "callback_data": "do|last_signal",
            },
        ],
        [
            {
                "text": "🗂 Recent Signals",
                "callback_data": "do|x_recent",
            },
            {
                "text": "💵 P&L 24h",
                "callback_data": "do|x_pnl24",
            },
        ],
        [
            {
                "text": "₿ BTC Pair",
                "callback_data": "do|pair",
            },
            {
                "text": "🔄 Choose Pair",
                "callback_data": "do|pairs",
            },
        ],
        [
            {
                "text": "🌊 Money Flow",
                "callback_data": "do|flow",
            },
            {
                "text": "🩺 Health",
                "callback_data": "do|x_health",
            },
        ],
        [
            {
                "text": "📊 CoinMarketCap",
                "callback_data": "do|x_cmc",
            },
            {
                "text": "🦎 CoinGecko",
                "callback_data": "do|x_cg",
            },
        ],
        [
            {
                "text": "🟢 Buy 1 BTC",
                "callback_data": "do|x_manual_buy",
            },
            {
                "text": "🔴 Sell 1 BTC",
                "callback_data": "do|x_manual_sell",
            },
        ],
        [
            {
                "text": "▶️ Resume Entries",
                "callback_data": "do|entries_on",
            },
            {
                "text": "⏸ Pause Entries",
                "callback_data": "do|x_pause",
            },
        ],
        [
            {
                "text": "🔁 Reconcile",
                "callback_data": "do|x_reconcile",
            },
            {
                "text": "🛡 Protection",
                "callback_data": "do|x_protection",
            },
        ],
        [
            {
                "text": "⚙️ System",
                "callback_data": "do|x_system",
            },
            {
                "text": "🧯 Recover Stream",
                "callback_data": "do|x_recover",
            },
        ],
        [
            {
                "text": "🚨 Emergency",
                "callback_data": "do|menu_emergency",
            },
            {
                "text": "📄 Logs",
                "callback_data": "do|logs",
            },
        ],
        [
            {
                "text": "ℹ️ Help",
                "callback_data": "do|help",
            },
        ],
    ]


def handle_message(message):
    chat = str(message.get("chat", {}).get("id", ""))
    user_id = message.get("from", {}).get("id")
    text = str(message.get("text", "")).strip()

    if text.lower() in {"/menu", "/owner"} and base.is_owner(user_id, chat):
        base.send(
            "₿ Bitcoin TestNet Control Panel\n\nFixed controls stay below the chat.",
            chat,
        )
        return

    action = resolve_button_action(text)
    if action and base.is_owner(user_id, chat):
        route(action, chat)
        return

    ORIGINAL_HANDLE_MESSAGE(message)


def send_builder(chat, fn):
    try:
        text = fn()
    except Exception as exc:
        text = (
            "Request failed safely.\n\n" +
            base._safe_error(exc)
        )

    base.send(
        text,
        chat,
    )


def route(action, chat, message_id=None):
    if action in {
        "home",
        "menu_dashboard",
    }:
        base.send(
            "₿ Bitcoin TestNet Control Panel\n\nFixed controls stay below the chat.",
            chat,
        )

    elif action == "status":
        send_builder(
            chat,
            status_text,
        )

    elif action == "balance":
        send_builder(
            chat,
            balance_text,
        )

    elif action == "x_open":
        send_builder(
            chat,
            open_trades_text,
        )

    elif action == "x_history":
        send_builder(
            chat,
            history_text,
        )

    elif action == "x_current":
        send_builder(
            chat,
            current_signal_text,
        )

    elif action == "last_signal":
        send_builder(
            chat,
            last_signal_text,
        )

    elif action == "x_recent":
        send_builder(
            chat,
            recent_signals_text,
        )

    elif action in {
        "x_pnl24",
        "profit",
    }:
        send_builder(
            chat,
            pnl24_text,
        )

    elif action == "pair":
        send_builder(
            chat,
            pair_text,
        )

    elif action == "pairs":
        base._show_pairs(
            chat,
            0,
        )

    elif action == "flow":
        send_builder(
            chat,
            flow_text,
        )

    elif action == "x_cmc":
        send_builder(
            chat,
            lambda: external_provider_text("coinmarketcap"),
        )

    elif action == "x_cg":
        send_builder(
            chat,
            lambda: external_provider_text("coingecko"),
        )


    elif action == "x_health":
        send_builder(
            chat,
            health_text,
        )

    elif action == "logs":
        send_builder(
            chat,
            logs_text,
        )

    elif action == "x_protection":
        send_builder(
            chat,
            protection_text,
        )

    elif action == "x_system":
        send_builder(
            chat,
            system_text,
        )

    elif action == "help":
        base.send(
            help_text(),
            chat,
        )

    elif action == "x_manual_buy":
        base._ask_confirm(
            chat,
            "CONFIRM manual 1 BTC buy",
            "manual_entry",
            {"symbol": (base.read_json(base.ACTIVE_PAIR_FILE, {}) or {}).get("symbol", "")},
            text=(
                "Place one 1 BTC Binance Spot TestNet BUY through the existing "
                "sidecar entry path and OTOCO protection? This does not create "
                "or alter an IctSmcStrategy signal."
            ),
        )

    elif action == "x_manual_sell":
        base._ask_confirm(
            chat,
            "CONFIRM manual 1 BTC sell",
            "emergency_exit",
            {"symbol": (base.read_json(base.ACTIVE_PAIR_FILE, {}) or {}).get("symbol", "")},
            text=(
                "Cancel the bot-owned active-pair protection and sell the current "
                "bot-owned TestNet BTC position through the verified emergency "
                "exit path?"
            ),
        )

    elif action == "x_pause":
        sc = base.sidecar_command(
            "entries",
            {
                "enabled": False,
            },
            wait=True,
        )

        ft = base.ft_call(
            "POST",
            "/pause",
        )

        base.send(
            "⏸ PAUSE RESULT\n\n"
            f"Sidecar entries: "
            f"{'OFF' if sc.get('ok') else 'ERROR'}\n"
            f"Freqtrade: "
            f"{'PAUSED' if ft.get('ok') else 'ERROR'}",
            chat,
        )

    elif action == "x_reconcile":
        r = base.sidecar_command(
            "reconcile",
            wait=True,
        )

        base.send(
            "🔁 RECONCILIATION\n\n" +
            (
                "PASS"
                if r.get("ok")
                else "FAILED"
            ) +
            "\n" +
            str(
                r.get(
                    "result",
                    "",
                )
            )[:3200],
            chat,
        )

    elif action == "x_recover":
        base._ask_confirm(
            chat,
            "CONFIRM recovery",
            "x_recover_confirmed",
            text=(
                "Restart Binance TestNet user stream, "
                "reconcile authenticated state, clear "
                "the reconnect risk pause only after "
                "reconciliation, then resume Freqtrade "
                "and sidecar entries?"
            ),
        )

    else:
        ORIGINAL_ROUTE(
            action,
            chat,
            message_id,
        )


def recover():
    ws = base.sidecar_command(
        "restart_stream",
        wait=True,
    )

    if ws.get("ok") is not True:
        return False, (
            "User stream restart failed: " +
            str(ws.get("result"))
        )

    rec = base.sidecar_command(
        "reconcile",
        wait=True,
    )

    if rec.get("ok") is not True:
        return False, (
            "Reconciliation failed: " +
            str(rec.get("result"))
        )

    clear = base.sidecar_command(
        "clear_risk_pause",
        wait=True,
    )

    if clear.get("ok") is not True:
        return False, (
            "Risk pause clear failed: " +
            str(clear.get("result"))
        )

    ft = base.ft_call(
        "POST",
        "/start",
    )

    if ft.get("ok") is not True:
        return False, (
            "Freqtrade start failed: " +
            str(ft.get("error"))
        )

    entries = base.sidecar_command(
        "entries",
        {
            "enabled": True,
        },
        wait=True,
    )

    if entries.get("ok") is not True:
        base.ft_call(
            "POST",
            "/pause",
        )

        return False, (
            "Sidecar refused entry arming; "
            "Freqtrade was paused again: " +
            str(entries.get("result"))
        )

    return True, (
        "User stream reconciled.\n"
        "Risk pause cleared.\n"
        "Freqtrade running.\n"
        "Sidecar entries armed."
    )


def confirm_action(action, args, chat):
    if action == "x_recover_confirmed":
        ok, text = recover()

        base.send(
            (
                "🧯 RECOVERY PASS\n\n"
                if ok
                else "🧯 RECOVERY FAILED\n\n"
            ) +
            text,
            chat,
        )

        return

    if action == "resume_entries":
        ft = base.ft_call(
            "POST",
            "/start",
        )

        if ft.get("ok") is not True:
            base.send(
                "▶️ RESUME FAILED\n\n"
                "Freqtrade did not start.",
                chat,
            )

            return

        sc = base.sidecar_command(
            "entries",
            {
                "enabled": True,
            },
            wait=True,
        )

        if sc.get("ok") is not True:
            base.ft_call(
                "POST",
                "/pause",
            )

            base.send(
                "▶️ RESUME FAILED\n\n"
                "Sidecar refused arming. "
                "Freqtrade was paused again.\n\n" +
                str(
                    sc.get(
                        "result",
                        "",
                    )
                )[:1200],
                chat,
            )

            return

        base.send(
            "▶️ RESUME PASS\n\n"
            "Freqtrade running.\n"
            "Sidecar entries armed.\n"
            "Only genuine IctSmcStrategy "
            "signals can create TestNet orders.",
            chat,
        )

        return

    ORIGINAL_CONFIRM(
        action,
        args,
        chat,
    )


def main():
    # This panel contains explicit virtual-fund/manual-test controls. The Live
    # package retains the standard mode-aware broker as its entrypoint.
    if os.getenv("EXECUTION_MODE", "simulation").lower() != "testnet":
        raise RuntimeError("the Testnet operator panel requires EXECUTION_MODE=testnet")
    base.send = send_fixed
    base.menu = menu
    base.dashboard_menu = menu
    base.route = route
    base._confirm_action = confirm_action
    base.handle_message = handle_message
    base.help_text = help_text
    start_signal_notifier()
    base.main()



if __name__ == "__main__":
    main()
