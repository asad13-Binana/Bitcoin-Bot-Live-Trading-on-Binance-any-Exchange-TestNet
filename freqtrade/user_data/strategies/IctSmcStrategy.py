# IctSmcStrategy.py — your ICT/SMC scalping logic, ported to Freqtrade.
#
# WHAT THIS IS: the original 1m/5m ICT/SMC signal formula hosted by
# Freqtrade for indicators, signal generation, and offline backtesting.
# Runtime Binance order ownership belongs exclusively to the execution sidecar.
#
# WHAT YOU MUST DO BEFORE ANY REAL MONEY:
#   1) freqtrade backtesting --strategy IctSmcStrategy --timeframe 1m ...
#   2) freqtrade trade --dry-run   (paper trade on live data for days)
#   3) only then, tiny live.
# The strategy still has to prove an edge in backtest. Treat any positive result
# with suspicion until look-ahead, recursive bias, fees, and slippage are checked.
#
# Install: https://www.freqtrade.io/en/stable/installation/
# Put this file in user_data/strategies/.

import logging
from pathlib import Path
import os, json, hashlib, tempfile
from datetime import datetime, timezone

import numpy as np
from pandas import DataFrame

from freqtrade.strategy import IStrategy, informative
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


logger = logging.getLogger(__name__)


class IctSmcStrategy(IStrategy):
    # ---- core config ----
    timeframe = "1m"
    can_short = False                 # SPOT only
    process_only_new_candles = True
    startup_candle_count = 210        # enough history for EMA200 context
    use_exit_signal = True
    exit_profit_only = False

    # ---- risk / exits (exchange-native, like your OCO trailing) ----
    # Hard stop (absolute floor). Trailing takes over once in profit.
    stoploss = -0.02                  # -2% hard floor; TUNE from backtests
    trailing_stop = True
    trailing_stop_positive = 0.004    # trail 0.4% behind price...
    trailing_stop_positive_offset = 0.006   # ...once +0.6% in profit
    trailing_only_offset_is_reached = True

    # minimal_roi = time-based FULL-exit thresholds (age_minutes -> min profit).
    # It exits the WHOLE position when reached — it is NOT a partial-TP ladder.
    minimal_roi = {
        "0": 0.012,    # take 1.2% immediately if available
        "20": 0.006,   # after 20 min, 0.6% is enough
        "45": 0.003,   # after 45 min, 0.3%
        "90": 0.0,     # after 90 min, exit at break-even+
    }

    # Place the stop ON the exchange (survives bot downtime), like your design.
    # Binance SPOT supports only STOP_LOSS_LIMIT for on-exchange stops
    # (verified in freqtrade source: stoploss_order_types = {"limit": ...}),
    # so "stoploss" MUST be "limit" here. Note the honest limitation of a
    # stop-LIMIT: in a violent gap through both stop and limit price it can
    # remain unfilled — the limit_ratio 0.99 gives a 1% fill buffer.
    # force_exit is MARKET so your Telegram /forceexit closes IMMEDIATELY.
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "force_entry": "limit",
        "force_exit": "market",
        "emergency_exit": "market",
        "stoploss": "limit",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 60,
        "stoploss_on_exchange_limit_ratio": 0.99,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    # Portfolio protections (tunable): pause after a stop-out, halt the pair
    # after repeated stops, and stand down on drawdown.
    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 5},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 60,
                "trade_limit": 3,
                "stop_duration_candles": 60,
                "only_per_pair": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 1440,
                "trade_limit": 10,
                "max_allowed_drawdown": 0.1,
                "stop_duration_candles": 720,
            },
        ]

    # ---- tunables (your thresholds) ----
    RVOL_MIN = 1.5
    RSI_MIN = 50.0

    # ---- Fail-closed BTC active-pair gate + Freqtrade-to-sidecar seam ----
    _pair_cache = (None, None)

    def _active_pair_path(self) -> Path:
        return Path(os.getenv('ACTIVE_PAIR_FILE', '/freqtrade/shared/pair/active_pair.json'))

    def _active_pair_state(self) -> dict:
        """Return the exact sidecar-owned BTC pair state or fail closed."""
        from services.common.market_policy import (
            canonical_pair,
            pair_config_hash,
            pair_state_hash,
        )
        p = self._active_pair_path()
        st = p.stat()
        cache_key = (st.st_mtime_ns, st.st_size)
        if self._pair_cache[1] is not None and self._pair_cache[0] == cache_key:
            return self._pair_cache[1]
        raw = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or raw.get('schema_version') != 1:
            raise ValueError('active pair schema_version 1 required')
        pair = canonical_pair(str(raw.get('pair', '')))
        if raw.get('base') != 'BTC' or raw.get('symbol') != pair.replace('/', ''):
            raise ValueError('active pair assets are inconsistent')
        if raw.get('pair_config_hash') != pair_config_hash(pair):
            raise ValueError('active pair configuration hash mismatch')
        if raw.get('state_hash') != pair_state_hash(raw):
            raise ValueError('active pair state hash mismatch')
        result = dict(raw, pair=pair)
        type(self)._pair_cache = (cache_key, result)
        return result

    def _is_active_pair(self, pair: str) -> bool:
        try:
            return str(pair).upper() == self._active_pair_state()['pair']
        except Exception as exc:
            self._seam_error('active BTC pair state unreadable (%s) — FAIL-CLOSED.' % exc)
            return False

    def _emit_signal(self,pair: str,row) -> None:
        inbox=Path(os.getenv('SIGNAL_INBOX','/freqtrade/shared/signals/inbox')); inbox.mkdir(parents=True,exist_ok=True)
        candle=row.get('date'); candle_time=candle.isoformat() if hasattr(candle,'isoformat') else str(candle)
        pair_state=self._active_pair_state()
        if pair != pair_state['pair']:
            raise ValueError('signal pair is not the authoritative active BTC pair')
        token=hashlib.sha256(
            f"{pair}|{pair_state['state_hash']}|{candle_time}|"
            "IctSmcStrategy|ema_vwap_pullback".encode()
        ).hexdigest()[:24]
        target=inbox/f'{token}.json'
        processed=Path(os.getenv('SIGNAL_PROCESSED','/freqtrade/shared/signals/processed'))
        rejected=Path(os.getenv('SIGNAL_REJECTED','/freqtrade/shared/signals/rejected'))
        # The sidecar moves files out of inbox. Check all three archives so the
        # same closed candle is not recreated on every bot loop.
        if target.exists() or any((folder/f'{token}.json').exists() for folder in (processed,rejected)):
            return
        if any(folder.exists() and next(folder.glob(f'{token}.*.json'),None) for folder in (processed,rejected)):
            return
        payload={'signal_id':token,'pair':pair,'symbol':pair_state['symbol'],'candle_time':candle_time,
          'generated_at':datetime.now(timezone.utc).isoformat(),'strategy':'IctSmcStrategy','entry_tag':str(row.get('enter_tag','')),
          'pair_state_hash':pair_state['state_hash'],
          'pair_generation':pair_state['generation'],
          'pair_config_hash':pair_state['pair_config_hash'],'payload':{
          'close':float(row.get('close',0) or 0),'rsi':float(row.get('rsi',0) or 0),'rvol':float(row.get('rvol',0) or 0),
          'adx':float(row.get('adx',0) or 0),'macdhist_5m':float(row.get('macdhist_5m',0) or 0)}}
        # V101-NEW-001: signals are HMAC-signed envelopes; the sidecar rejects
        # anything unsigned. If signing is unavailable, NO signal is emitted.
        try:
            from services.common.envelope import BUS_SIGNAL, sign_envelope
            signed=sign_envelope(producer='freqtrade-strategy',purpose=BUS_SIGNAL,payload=payload,
                                 ttl_seconds=int(os.getenv('MAX_SIGNAL_AGE_SECONDS','180'))+60)
        except Exception as exc:
            self._seam_error('signal signing unavailable — FAIL-CLOSED, no emission: %s'%exc)
            return
        fd,tmp=tempfile.mkstemp(prefix=token+'.',suffix='.tmp',dir=inbox)
        try:
            with os.fdopen(fd,'w') as f: json.dump(signed,f,sort_keys=True); f.flush(); os.fsync(f.fileno())
            os.chmod(tmp,0o640)
            os.replace(tmp,target)
            type(self)._seam_state['emitted']=type(self)._seam_state.get('emitted',0)+1
        finally:
            try:
                if os.path.exists(tmp): os.unlink(tmp)
            except OSError: pass

    # ---- signal-seam observability (V101-NEW-004) ----
    _seam_state={'last_error':'','count':0,'last_log':0.0,'emitted':0}

    def _seam_error(self,message: str)->None:
        """Escalating, rate-limited seam error reporting (never DEBUG-only)."""
        state=type(self)._seam_state; import time as _t
        state['count']=state['count']+1 if state['last_error']==message else 1
        state['last_error']=message
        if _t.time()-state['last_log']>60:
            state['last_log']=_t.time()
            log_fn=logger.error if state['count']>=5 else logger.warning
            log_fn('signal seam: %s (repeat x%d)',message,state['count'])

    def _seam_heartbeat(self,ok: bool,whitelist: int,active: int,error: str='',
                        pair_state: dict | None=None)->None:
        """Durable heartbeat consumed by the container healthcheck."""
        try:
            hb=Path(os.getenv('SIGNAL_HEARTBEAT','/freqtrade/shared/freqtrade/signal_seam_heartbeat.json'))
            hb.parent.mkdir(parents=True,exist_ok=True)
            state=type(self)._seam_state
            payload={'ts':datetime.now(timezone.utc).isoformat(),'ok':bool(ok),
                     'whitelist':int(whitelist),'active_pairs':int(active),
                     'pair':(pair_state or {}).get('pair'),
                     'pair_generation':(pair_state or {}).get('generation'),
                     'pair_config_hash':(pair_state or {}).get('pair_config_hash'),
                     'emitted_total':int(state.get('emitted',0)),
                     'last_error':error or state.get('last_error',''),
                     'error_repeat':int(state.get('count',0))}
            fd,tmp=tempfile.mkstemp(dir=hb.parent,suffix='.tmp')
            with os.fdopen(fd,'w') as f: json.dump(payload,f,sort_keys=True); f.flush(); os.fsync(f.fileno())
            os.chmod(tmp,0o640)
            os.replace(tmp,hb)
        except Exception:
            pass

    def _runmode_value(self) -> str:
        mode = (getattr(self, "config", None) or {}).get("runmode", "")
        return str(getattr(mode, "value", mode)).strip().lower().replace("-", "_")

    def bot_loop_start(self,current_time=None,**kwargs)->None:
        # Offline engines can call this hook once per historical candle.
        # They must evaluate the strategy formula without emitting runtime
        # envelopes or rewriting the live seam heartbeat.
        if self._runmode_value() in {"backtest", "hyperopt", "edge"}:
            return
        wl_count=active_count=0
        pair_state=None
        try:
            pair_state=self._active_pair_state()
            wl=self.dp.current_whitelist(); active=[
                p for p in wl if str(p).upper()==pair_state['pair']]
            wl_count,active_count=len(wl),len(active)
            key=','.join(active); last_key,last_ts=getattr(self,'_wl_state',('',0.0)); import time as _t
            if key!=last_key or (_t.time()-last_ts)>1800:
                self._wl_state=(key,_t.time())
                if key!=last_key:self.dp.send_msg('Active Bitcoin pair: '+(key or 'NONE (entries fail closed)'))
            for pair in active:
                df,_=self.dp.get_analyzed_dataframe(pair,self.timeframe)
                if df is None or df.empty: continue
                row=df.iloc[-1]
                # Freqtrade may leave enter_long as NaN before an entry signal.
                # Direct equality is NaN-safe; int(float("nan")) raises and
                # would turn a normal no-signal candle into a seam failure.
                if row.get('enter_long', 0) == 1:
                    self._emit_signal(pair,row)
            self._seam_heartbeat(True,wl_count,active_count,pair_state=pair_state)
        except Exception as exc:
            # V101-NEW-004 fix: a persistent seam failure is escalated and
            # visible to the healthcheck instead of vanishing at DEBUG level.
            self._seam_error(str(exc))
            self._seam_heartbeat(False,wl_count,active_count,error=str(exc),
                                 pair_state=pair_state)

    def bot_start(self,**kwargs)->None:
        logger.warning('Bitcoin signal-engine mode: runtime Freqtrade order submission is disabled; execution sidecar owns Binance orders.')

    def confirm_trade_entry(self,pair: str,order_type: str,amount:float,rate:float,time_in_force:str,current_time,entry_tag,side:str,**kwargs)->bool:
        """Permit offline optimization only; deny every runtime order path.

        Freqtrade's backtester calls this callback. Returning ``False`` in all
        modes silently produces zero trades, so BACKTEST/HYPEROPT must be
        allowed. LIVE and DRY_RUN remain denied because the execution sidecar
        is the sole Binance order owner in both testnet and live deployments.
        """
        mode = (getattr(self, "config", None) or {}).get("runmode", "")
        mode_value = str(getattr(mode, "value", mode)).strip().lower().replace("-", "_")
        return mode_value in {"backtest", "hyperopt"}

    # ================= 5-minute trend filter (#9) =================
    @informative("5m")
    def populate_indicators_5m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)   # context/backdrop
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        return dataframe

    # ================= 1-minute entry logic (#1) =================
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)

        # RSI as a MOMENTUM filter (>50 & rising), not oversold
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["rsi_rising"] = dataframe["rsi"] > dataframe["rsi"].shift(1)

        # 1m MACD fast 5/13/6 (soft confirmation / boost)
        macd = ta.MACD(dataframe, fastperiod=5, slowperiod=13, signalperiod=6)
        dataframe["macdhist"] = macd["macdhist"]

        # Rolling VWAP over the last 200 bars (an approximation — NOT a true
        # session-anchored VWAP; treat it as a dynamic value baseline)
        dataframe["vwap"] = qtpylib.rolling_vwap(dataframe, window=200)

        # RVOL = current volume / 20-bar average
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()
        dataframe["rvol"] = dataframe["volume"] / dataframe["vol_ma"]

        dataframe["adx"] = ta.ADX(dataframe)

        # pullback: a recent low tagged the EMA9/EMA21 value zone
        zone = dataframe[["ema9", "ema21"]].max(axis=1)
        # a pullback = any of the last 3 bars traded down into its own EMA zone
        touched = (dataframe["low"] <= zone)
        dataframe["pullback"] = touched.rolling(3).max() > 0
        dataframe["ema9_rising"] = dataframe["ema9"] >= dataframe["ema9"].shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            # --- 5m HARD trend gate (#9) ---
            (dataframe["ema9_5m"] > dataframe["ema21_5m"]),
            (dataframe["ema21_5m"] > dataframe["ema50_5m"]),
            (dataframe["close"] > dataframe["ema50_5m"]),
            (dataframe["macdhist_5m"] > 0),                 # 5m MACD bullish (hard)
            # --- 1m entry (#1) ---
            (dataframe["close"] > dataframe["vwap"]),       # above VWAP value
            (dataframe["pullback"]),                        # pulled back to EMA9/21
            (dataframe["close"] > dataframe["ema9"]),       # reclaim close
            (dataframe["ema9_rising"]),
            # --- momentum + participation ---
            (dataframe["rsi"] > self.RSI_MIN),
            (dataframe["rsi_rising"]),
            (dataframe["rvol"] >= self.RVOL_MIN),
            # 1m MACD (5/13/6) is computed for reference/analysis only — it has
            # NO effect on entries in this version. To make it a gate, add:
            #   (dataframe["macdhist"] > 0),
            (dataframe["adx"] > 20),
            (dataframe["volume"] > 0),
        ]
        dataframe.loc[
            np.logical_and.reduce(conditions),
            ["enter_long", "enter_tag"],
        ] = (1, "ema_vwap_pullback")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exits are handled by minimal_roi + trailing stop + exchange stoploss.
        # A light structural exit: price loses VWAP AND 5m turns bearish.
        dataframe.loc[
            (
                (dataframe["close"] < dataframe["vwap"])
                & (dataframe["macdhist_5m"] < 0)
            ),
            ["exit_long", "exit_tag"],
        ] = (1, "lost_vwap_5m_bear")
        return dataframe
