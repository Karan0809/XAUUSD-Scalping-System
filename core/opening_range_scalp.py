import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple

import pandas as pd
import numpy as np

from core.institutional_zone import InstitutionalZoneDetector, InstitutionalZone

logger = logging.getLogger(__name__)

SESSION_OPENINGS: dict[str, tuple[int, int]] = {
    "asia": (0, 0),
    "london": (9, 0),
    "ny": (13, 30),
}
SESSION_RANGES: dict[str, tuple[int, int]] = {
    "asia": (0, 9),
    "london": (9, 12),
    "ny": (13, 16),
}


class OpeningRangeScalp:
    def __init__(
        self,
        zone_detector: Optional[InstitutionalZoneDetector] = None,
    ):
        self._zone_detector = zone_detector
        self._current_date: Optional[str] = None
        self._current_session: Optional[str] = None
        self._range_high: Optional[float] = None
        self._range_low: Optional[float] = None
        self._range_established: bool = False
        self._market_structure: Optional[str] = None
        self._entry_triggered: bool = False
        self._breakout_dir: Optional[str] = None
        self._poi_high: Optional[float] = None
        self._poi_low: Optional[float] = None
        self._best_zone: Optional[InstitutionalZone] = None
        self._htf_aligned: bool = False
        self._breakout_fvg: Optional[Tuple[float, float]] = None
        self._swing_broken: bool = False

    def reset_entry(self) -> None:
        self._entry_triggered = False
        self._breakout_dir = None
        self._poi_high = None
        self._poi_low = None
        self._best_zone = None
        self._htf_aligned = False
        self._breakout_fvg = None
        self._swing_broken = False

    def reset(self) -> None:
        self._current_date = None
        self._current_session = None
        self._range_high = None
        self._range_low = None
        self._range_established = False
        self._market_structure = None
        self._entry_triggered = False
        self._breakout_dir = None
        self._poi_high = None
        self._poi_low = None
        self._best_zone = None
        self._htf_aligned = False
        self._breakout_fvg = None
        self._swing_broken = False

    def _detect_session(self, current_time: datetime) -> Optional[str]:
        t = current_time.hour
        for session, (start, end) in SESSION_RANGES.items():
            if start <= t < end:
                return session
        return None

    def _is_after_opening_range(self, current_time: datetime) -> bool:
        or_hour, or_min = self._get_opening_range_candle_time()
        or_end = or_hour * 60 + or_min + 15
        current = current_time.hour * 60 + current_time.minute
        return current >= or_end

    def _get_opening_range_candle_time(self) -> Tuple[int, int]:
        return SESSION_OPENINGS.get(self._current_session, (13, 30))

    def _establish_opening_range(self, df_15min: pd.DataFrame, current_time: datetime) -> bool:
        if self._range_established:
            return True
        if not self._is_after_opening_range(current_time):
            return False
        if df_15min is None or df_15min.empty:
            return False

        or_hour, or_min = self._get_opening_range_candle_time()
        or_start = current_time.replace(hour=or_hour, minute=or_min, second=0, microsecond=0)
        or_end = or_start + timedelta(minutes=15)

        range_candle = df_15min[(df_15min.index >= or_start) & (df_15min.index < or_end)]
        if len(range_candle) < 1:
            return False

        self._range_high = range_candle["high"].max()
        self._range_low = range_candle["low"].min()
        self._range_established = True
        logger.info(
            f"Opening range: H={self._range_high:.2f} L={self._range_low:.2f} "
            f"({or_start.strftime('%H:%M')}-{or_end.strftime('%H:%M')} UTC)"
        )
        return True

    def _determine_market_structure(self, df_15min: pd.DataFrame) -> Optional[str]:
        if df_15min is None or len(df_15min) < 10:
            return None

        recent = df_15min.tail(10)
        highs = recent["high"].values
        lows = recent["low"].values

        hh = sum(1 for i in range(2, len(highs)) if highs[i] > highs[i-1] and highs[i] > highs[i-2])
        hl = sum(1 for i in range(2, len(lows)) if lows[i] > lows[i-1] and lows[i] > lows[i-2])
        lh = sum(1 for i in range(2, len(highs)) if highs[i] < highs[i-1] and highs[i] < highs[i-2])
        ll = sum(1 for i in range(2, len(lows)) if lows[i] < lows[i-1] and lows[i] < lows[i-2])

        if hh >= 3 and hl >= 3:
            self._market_structure = "uptrend"
        elif ll >= 3 and lh >= 3:
            self._market_structure = "downtrend"
        else:
            self._market_structure = "ranging"

        logger.debug(f"Market structure: {self._market_structure} (HH={hh} HL={hl} LL={ll} LH={lh})")
        return self._market_structure

    def _detect_breakout(self, df_5min: pd.DataFrame) -> Optional[Tuple[str, int]]:
        if df_5min is None or len(df_5min) < 3:
            return None

        recent = df_5min.tail(10)
        for i in range(len(recent)):
            candle = recent.iloc[i]
            if (
                candle["open"] > self._range_high
                and candle["close"] > self._range_high
                and candle["close"] > candle["open"]
            ):
                self._breakout_dir = "buy"
                actual_idx = len(df_5min) - len(recent) + i
                logger.debug(f"Buy breakout at {recent.index[i]}: O={candle['open']:.2f} C={candle['close']:.2f}")
                return ("buy", actual_idx)
            elif (
                candle["open"] < self._range_low
                and candle["close"] < self._range_low
                and candle["close"] < candle["open"]
            ):
                self._breakout_dir = "sell"
                actual_idx = len(df_5min) - len(recent) + i
                logger.debug(f"Sell breakout at {recent.index[i]}: O={candle['open']:.2f} C={candle['close']:.2f}")
                return ("sell", actual_idx)
        return None

    def _find_poi(self, df_5min: pd.DataFrame, breakout_idx: pd.Timestamp, direction: str) -> Optional[Tuple[float, float]]:
        df_before = df_5min[df_5min.index <= breakout_idx]
        if len(df_before) < 5:
            return None

        pre_breakout = df_before.tail(8)
        for i in range(len(pre_breakout) - 2):
            c1, c2, c3 = pre_breakout.iloc[i], pre_breakout.iloc[i+1], pre_breakout.iloc[i+2]
            if direction == "buy":
                if c2["high"] < c1["low"] and c2["low"] > c3["high"]:
                    poi_high = c1["low"]
                    poi_low = c3["high"]
                    self._poi_high = poi_high
                    self._poi_low = poi_low
                    return (poi_low, poi_high)
            else:
                if c2["low"] > c1["high"] and c2["high"] < c3["low"]:
                    poi_high = c3["low"]
                    poi_low = c1["high"]
                    self._poi_high = poi_high
                    self._poi_low = poi_low
                    return (poi_low, poi_high)

        swing_low = pre_breakout["low"].min()
        swing_high = pre_breakout["high"].max()
        if direction == "buy":
            self._poi_high = pre_breakout.iloc[-1]["high"]
            self._poi_low = swing_low
            return (swing_low, pre_breakout.iloc[-1]["high"])
        else:
            self._poi_high = swing_high
            self._poi_low = pre_breakout.iloc[-1]["low"]
            return (pre_breakout.iloc[-1]["low"], swing_high)

    def _check_pullback(self, df_5min: pd.DataFrame, poi: Tuple[float, float], direction: str) -> Optional[Dict[str, float]]:
        if df_5min is None or len(df_5min) < 2:
            return None

        recent = df_5min.tail(8)
        zl, zh = poi
        for idx, row in recent.iterrows():
            if direction == "buy":
                if zl <= row["low"] <= zh and row["close"] > row["open"]:
                    fib_retrace = (zh - row["close"]) / max(zh - zl, 0.001)
                    if 0.50 <= fib_retrace <= 0.618:
                        return {"entry": row["close"], "sl": row["low"] - 0.03}
            else:
                if zl <= row["high"] <= zh and row["close"] < row["open"]:
                    fib_retrace = (row["close"] - zl) / max(zh - zl, 0.001)
                    if 0.50 <= fib_retrace <= 0.618:
                        return {"entry": row["close"], "sl": row["high"] + 0.03}
        return None

    def _check_range_reversal(self, df_5min: pd.DataFrame) -> Optional[Dict[str, Any]]:
        if df_5min is None or len(df_5min) < 2:
            return None

        recent = df_5min.tail(5)
        for idx, row in recent.iterrows():
            wick_bottom = min(row["close"], row["open"]) - row["low"]
            wick_top = row["high"] - max(row["close"], row["open"])
            total = row["high"] - row["low"]
            if total <= 0:
                continue

            if row["low"] <= self._range_low * 1.0005:
                if wick_bottom / total >= 0.5 and row["close"] > row["open"]:
                    return {"direction": "buy", "price": row["close"], "time": idx}
            if row["high"] >= self._range_high * 0.9995:
                if wick_top / total >= 0.5 and row["close"] < row["open"]:
                    return {"direction": "sell", "price": row["close"], "time": idx}
        return None

    def _find_fvg_near_range(self, df_5min: pd.DataFrame, direction: str) -> Optional[float]:
        if df_5min is None or len(df_5min) < 4:
            return None

        recent = df_5min.tail(12)
        for i in range(len(recent) - 2):
            c1 = recent.iloc[i]
            c2 = recent.iloc[i + 1]
            c3 = recent.iloc[i + 2]
            if direction == "buy":
                if c1["high"] < c3["low"]:
                    fvg_mid = (c1["high"] + c3["low"]) / 2.0
                    if fvg_mid < self._range_high * 1.05:
                        logger.debug(f"Bullish FVG near range: {fvg_mid:.2f}")
                        return fvg_mid
            else:
                if c1["low"] > c3["high"]:
                    fvg_mid = (c1["low"] + c3["high"]) / 2.0
                    if fvg_mid > self._range_low * 0.95:
                        logger.debug(f"Bearish FVG near range: {fvg_mid:.2f}")
                        return fvg_mid
        return None

    def _find_fvg_anywhere(self, df_5min: pd.DataFrame, direction: str) -> Optional[float]:
        if df_5min is None or len(df_5min) < 4:
            return None
        recent = df_5min.tail(12)
        for i in range(len(recent) - 2):
            c1 = recent.iloc[i]
            c2 = recent.iloc[i + 1]
            c3 = recent.iloc[i + 2]
            if direction == "buy":
                if c1["high"] < c3["low"]:
                    return (c1["high"] + c3["low"]) / 2.0
            else:
                if c1["low"] > c3["high"]:
                    return (c1["low"] + c3["high"]) / 2.0
        return None

    def _check_momentum_breakout(self, df_5min: pd.DataFrame, breakout_idx: int, direction: str) -> bool:
        return False

    def _calculate_ema(self, df: pd.DataFrame, period: int) -> pd.Series:
        return df["close"].ewm(span=period, adjust=False).mean()

    def _check_bos(self, df: pd.DataFrame, lookback: int = 8) -> Tuple[bool, bool]:
        if len(df) < lookback:
            return (False, False)
        recent = df.tail(lookback)
        highs = recent["high"].values
        lows = recent["low"].values
        bull_bos = any(highs[i] > highs[i-1] and highs[i] > highs[i-2] for i in range(2, len(highs)))
        bear_bos = any(lows[i] < lows[i-1] and lows[i] < lows[i-2] for i in range(2, len(lows)))
        return (bull_bos, bear_bos)

    def _check_htf_alignment(self, df_15min: pd.DataFrame) -> Tuple[bool, str]:
        if df_15min is None or len(df_15min) < 60:
            return (False, "none")

        ema50 = self._calculate_ema(df_15min, 50)
        ema200 = self._calculate_ema(df_15min, 200)
        current_close = df_15min["close"].iloc[-1]
        ema50_val = ema50.iloc[-1]
        ema200_val = ema200.iloc[-1]

        if pd.isna(ema50_val) or pd.isna(ema200_val):
            return (False, "none")

        bull_bos, bear_bos = self._check_bos(df_15min)
        recent = df_15min.tail(10)
        highs = recent["high"].values
        lows = recent["low"].values
        hh = sum(1 for i in range(2, len(highs)) if highs[i] > highs[i-1] and highs[i] > highs[i-2])
        hl = sum(1 for i in range(2, len(lows)) if lows[i] > lows[i-1] and lows[i] > lows[i-2])
        ll = sum(1 for i in range(2, len(highs)) if highs[i] < highs[i-1] and highs[i] < highs[i-2])
        lh = sum(1 for i in range(2, len(lows)) if lows[i] < lows[i-1] and lows[i] < lows[i-2])

        crosses_ema = False
        for i in range(-5, 0):
            prev = df_15min["close"].iloc[i-1]
            curr = df_15min["close"].iloc[i]
            prev_ema = ema50.iloc[i-1]
            curr_ema = ema50.iloc[i]
            if (prev <= prev_ema and curr > curr_ema) or (prev >= prev_ema and curr < curr_ema):
                crosses_ema = True
                break

        if current_close > ema50_val > ema200_val and hh >= 2 and hl >= 2 and bull_bos and not crosses_ema:
            self._htf_aligned = True
            return (True, "uptrend")
        elif current_close < ema50_val < ema200_val and ll >= 2 and lh >= 2 and bear_bos and not crosses_ema:
            self._htf_aligned = True
            return (True, "downtrend")
        return (False, "ranging")

    def _check_fvg_on_breakout(self, df_5min: pd.DataFrame, breakout_idx: int, direction: str) -> bool:
        if df_5min is None or breakout_idx < 1:
            return False
        c_prev = df_5min.iloc[breakout_idx - 1]
        c_bk = df_5min.iloc[breakout_idx]
        if direction == "buy":
            if c_bk["open"] > c_prev["high"]:
                self._breakout_fvg = (c_prev["high"], c_bk["open"])
                return True
        else:
            if c_bk["open"] < c_prev["low"]:
                self._breakout_fvg = (c_bk["open"], c_prev["low"])
                return True
        if breakout_idx + 2 < len(df_5min):
            c2 = df_5min.iloc[breakout_idx + 1]
            c3 = df_5min.iloc[breakout_idx + 2]
            if direction == "buy":
                if c2["low"] > c_bk["high"]:
                    self._breakout_fvg = (c_bk["high"], c2["low"])
                    return True
                if c3["low"] > c2["high"]:
                    self._breakout_fvg = (c2["high"], c3["low"])
                    return True
            else:
                if c2["high"] < c_bk["low"]:
                    self._breakout_fvg = (c2["high"], c_bk["low"])
                    return True
                if c3["high"] < c2["low"]:
                    self._breakout_fvg = (c3["high"], c2["low"])
                    return True
        return False

    def _check_swing_break(self, df_5min: pd.DataFrame, direction: str) -> bool:
        if df_5min is None or len(df_5min) < 12:
            return False
        pre = df_5min.tail(12).iloc[:-2]
        if direction == "buy":
            recent_high = pre["high"].max()
            self._swing_broken = df_5min["high"].iloc[-1] > recent_high
        else:
            recent_low = pre["low"].min()
            self._swing_broken = df_5min["low"].iloc[-1] < recent_low
        return self._swing_broken

    def _check_slow_momentum(self, df_5min: pd.DataFrame, n: int = 5) -> bool:
        if df_5min is None or len(df_5min) < n:
            return False
        recent = df_5min.tail(n)
        bodies = [abs(r["close"] - r["open"]) for _, r in recent.iterrows()]
        directions = [1 if r["close"] > r["open"] else -1 for _, r in recent.iterrows()]
        mixed = len(set(directions)) > 1
        avg_body = np.mean(bodies)
        max_body = max(bodies)
        no_aggressive = max_body <= avg_body * 2.5 if avg_body > 0 else True
        return mixed and no_aggressive

    def _check_reaction(self, df_5min: pd.DataFrame, direction: str) -> bool:
        if df_5min is None or len(df_5min) < 3:
            return False
        c0 = df_5min.iloc[-3]
        c1 = df_5min.iloc[-2]
        c2 = df_5min.iloc[-1]
        if direction == "buy":
            body0 = abs(c0["close"] - c0["open"])
            body1 = abs(c1["close"] - c1["open"])
            body2 = abs(c2["close"] - c2["open"])
            bullish = c2["close"] > c2["open"]
            if not bullish:
                return False
            engulfing = c2["close"] > c0["high"] and c2["open"] < c0["low"] and body2 > body0 * 1.5
            if engulfing:
                return True
            wick_rejection = (min(c2["close"], c2["open"]) - c2["low"]) > body2 * 0.5
            if wick_rejection and c2["close"] > c2["open"]:
                return True
            mss = c2["close"] > c1["high"] and c1["close"] > c0["high"] and c1["close"] > c1["open"]
            if mss:
                return True
            strong_green = bullish and body2 > body1 * 1.3 and c2["close"] > max(c0["close"], c1["close"])
            if strong_green:
                return True
            simple_bullish = bullish and body2 > 0 and c2["close"] > c1["close"]
            if simple_bullish:
                return True
            return False
        else:
            body0 = abs(c0["close"] - c0["open"])
            body1 = abs(c1["close"] - c1["open"])
            body2 = abs(c2["close"] - c2["open"])
            bearish = c2["close"] < c2["open"]
            if not bearish:
                return False
            engulfing = c2["close"] < c0["low"] and c2["open"] > c0["high"] and body2 > body0 * 1.5
            if engulfing:
                return True
            wick_rejection = (c2["high"] - max(c2["close"], c2["open"])) > body2 * 0.5
            if wick_rejection and c2["close"] < c2["open"]:
                return True
            mss = c2["close"] < c1["low"] and c1["close"] < c0["low"] and c1["close"] < c1["open"]
            if mss:
                return True
            strong_red = bearish and body2 > body1 * 1.3 and c2["close"] < min(c0["close"], c1["close"])
            if strong_red:
                return True
            simple_bearish = bearish and body2 > 0 and c2["close"] < c1["close"]
            if simple_bearish:
                return True
            return False



    def _check_fib_discount(self, df_5min: pd.DataFrame, price: float, direction: str, poi: Optional[Tuple[float, float]] = None) -> bool:
        if df_5min is None or len(df_5min) < 5:
            return True
        if poi is not None and poi != (0, 0):
            if direction == "buy":
                swing_low = poi[0]
                swing_high = max(df_5min["high"].tail(12).max(), poi[1])
            else:
                swing_high = poi[1]
                swing_low = min(df_5min["low"].tail(12).min(), poi[0])
        else:
            pre = df_5min.tail(20)
            if direction == "buy":
                swing_low = pre["low"].min()
                swing_high = pre["high"].max()
            else:
                swing_low = pre["low"].min()
                swing_high = pre["high"].max()
        if swing_high - swing_low < 0.01:
            return True
        if direction == "buy":
            fib_level = (swing_high - price) / (swing_high - swing_low)
        else:
            fib_level = (price - swing_low) / (swing_high - swing_low)
        return fib_level <= 0.618

    def _run_orb_pipeline(
        self,
        df_5min: pd.DataFrame,
        df_15min: pd.DataFrame,
        current_time: datetime,
    ) -> Optional[Dict[str, Any]]:
        if not self._establish_opening_range(df_15min, current_time):
            return None

        if self._market_structure is None:
            self._determine_market_structure(df_15min)

        if self._market_structure is None:
            return None

        range_width = self._range_high - self._range_low

        if self._market_structure == "ranging":
            reversal = self._check_range_reversal(df_5min)
            if reversal:
                entry = reversal["price"]
                direction = reversal["direction"]
                if direction == "buy":
                    sl = self._range_low - 0.05
                    sl = min(sl, entry - 0.50)
                    tp = entry + range_width
                else:
                    sl = self._range_high + 0.05
                    sl = max(sl, entry + 0.50)
                    tp = entry - range_width
                logger.info(
                    f"ORB range reversal {direction.upper()} at {entry:.2f} "
                    f"SL={sl:.2f} TP={tp:.2f} width={range_width:.2f}"
                )
                return {
                    "type": "opening_range_scalp",
                    "direction": direction,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "setup": "range_reversal",
                }
            return None

        htf_aligned, htf_dir = self._check_htf_alignment(df_15min) if not self._htf_aligned else (True, self._market_structure)
        if not htf_aligned:
            return None

        expected_dir = "buy" if self._market_structure == "uptrend" else "sell"

        breakout = self._detect_breakout(df_5min)
        if breakout is None:
            return None
        breakout_dir, breakout_idx = breakout

        if breakout_dir != expected_dir:
            return None

        if not self._check_swing_break(df_5min, breakout_dir):
            return None

        best_zone = None
        if self._zone_detector is not None:
            zone_dir = "demand" if breakout_dir == "buy" else "supply"
            best_zone = self._zone_detector.get_best_zone(zone_dir, df_5min["close"].iloc[-1])
            if best_zone is not None:
                self._best_zone = best_zone

        poi = self._find_poi(df_5min, df_5min.index[-1], breakout_dir)
        if poi is None and best_zone is None:
            return None

        pullback_poi = poi
        if pullback_poi is None and best_zone is not None:
            pullback_poi = (best_zone.zone_low, best_zone.zone_high)

        pullback = self._check_pullback(df_5min, pullback_poi, breakout_dir)
        setup = "breakout_pullback"
        entry_price = None
        sl = None

        if pullback is not None:
            entry_price = pullback["entry"]
            sl = pullback["sl"]
            if not self._check_slow_momentum(df_5min):
                return None
            if not self._check_fib_discount(df_5min, entry_price, breakout_dir, pullback_poi):
                return None
            if not self._check_reaction(df_5min, breakout_dir):
                return None
        else:
            fvg_entry = self._find_fvg_near_range(df_5min, breakout_dir)
            if fvg_entry is not None:
                entry_price = fvg_entry
                if breakout_dir == "buy":
                    sl = self._range_high - 0.03
                else:
                    sl = self._range_low + 0.03
                setup = "aggressive_fvg"
            else:
                return None

        max_zone_dist = 0.50
        if best_zone is not None:
            if breakout_dir == "buy":
                zone_sl = best_zone.zone_low - 0.03
                zone_dist = abs(entry_price - zone_sl)
                if zone_dist <= max_zone_dist:
                    sl = zone_sl if sl is None else max(sl, zone_sl)
            else:
                zone_sl = best_zone.zone_high + 0.03
                zone_dist = abs(entry_price - zone_sl)
                if zone_dist <= max_zone_dist:
                    sl = zone_sl if sl is None else min(sl, zone_sl)

        # Ensure minimum SL distance for breathing room
        if sl is not None:
            min_sl = 1.00
            if breakout_dir == "buy":
                sl = min(sl, entry_price - min_sl)
            else:
                sl = max(sl, entry_price + min_sl)

        sl_distance = abs(entry_price - sl) if sl else 0.01
        if breakout_dir == "buy":
            tp = entry_price + sl_distance * 10.0
        else:
            tp = entry_price - sl_distance * 10.0

        logger.info(
            f"ORB scalp {breakout_dir.upper()} at {entry_price:.2f} "
            f"SL={sl:.2f} TP={tp:.2f} trail={sl_distance:.2f} zone={best_zone is not None} ({setup})"
        )
        return {
            "type": "opening_range_scalp",
            "direction": breakout_dir,
            "entry": entry_price,
            "sl": sl,
            "tp": tp,
            "setup": setup,
            "zone_id": id(best_zone) if best_zone else None,
        }

    def _run_free_trade(
        self,
        df_5min: pd.DataFrame,
        df_15min: pd.DataFrame,
        current_time: datetime,
    ) -> Optional[Dict[str, Any]]:
        if df_5min is None or len(df_5min) < 20 or self._zone_detector is None:
            return None

        current_price = df_5min["close"].iloc[-1]

        htf_aligned, htf_dir = self._check_htf_alignment(df_15min)
        if not htf_aligned:
            return None

        direction = "buy" if htf_dir == "uptrend" else "sell"

        if not self._check_swing_break(df_5min, direction):
            return None

        zone_dir = "demand" if direction == "buy" else "supply"
        best_zone = self._zone_detector.get_best_zone(zone_dir, current_price)
        if best_zone is None:
            return None

        pullback_poi = (best_zone.zone_low, best_zone.zone_high)

        fvg_entry = self._find_fvg_anywhere(df_5min, direction)

        pullback = self._check_pullback(df_5min, pullback_poi, direction)
        entry_price = None
        sl = None
        setup = None

        max_free_sl = 1.00
        if pullback is not None:
            entry_price = pullback["entry"]
            sl = pullback["sl"]
            if not self._check_slow_momentum(df_5min):
                return None
            if not self._check_fib_discount(df_5min, entry_price, direction, pullback_poi):
                return None
            if not self._check_reaction(df_5min, direction):
                return None
            setup = "free_pullback"
        elif fvg_entry is not None:
            entry_price = fvg_entry
            if direction == "buy":
                sl = entry_price - max_free_sl
            else:
                sl = entry_price + max_free_sl
            setup = "free_fvg"
        else:
            return None

        if direction == "buy":
            zone_sl = best_zone.zone_low - 0.03
            zone_dist = abs(entry_price - zone_sl)
            if zone_dist <= max_free_sl:
                sl = zone_sl if sl is None else max(sl, zone_sl)
        else:
            zone_sl = best_zone.zone_high + 0.03
            zone_dist = abs(entry_price - zone_sl)
            if zone_dist <= max_free_sl:
                sl = zone_sl if sl is None else min(sl, zone_sl)

        # Ensure minimum SL for breathing room
        if sl is not None:
            if direction == "buy":
                sl = min(sl, entry_price - max_free_sl)
            else:
                sl = max(sl, entry_price + max_free_sl)

        sl_distance = abs(entry_price - sl)
        if sl_distance < 0.01:
            return None
        if direction == "buy":
            tp = entry_price + sl_distance * 10.0
        else:
            tp = entry_price - sl_distance * 10.0

        logger.info(
            f"Free trade {direction.upper()} at {entry_price:.2f} "
            f"SL={sl:.2f} TP={tp:.2f} ({setup}) zone={best_zone.zone_low:.2f}-{best_zone.zone_high:.2f}"
        )
        return {
            "type": "opening_range_scalp",
            "direction": direction,
            "entry": entry_price,
            "sl": sl,
            "tp": tp,
            "setup": setup,
            "zone_id": id(best_zone),
        }

    def analyze(
        self,
        df_5min: pd.DataFrame,
        df_15min: pd.DataFrame,
        current_time: datetime,
        session: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        date_str = current_time.strftime("%Y-%m-%d")
        if date_str != self._current_date:
            self.reset()
            self._current_date = date_str

        if session is not None and session != self._current_session:
            self.reset()
            self._current_session = session

        if self._entry_triggered:
            return None

        if session is not None:
            signal = self._run_orb_pipeline(df_5min, df_15min, current_time)
            if signal is not None:
                self._entry_triggered = True
                return signal

        signal = self._run_free_trade(df_5min, df_15min, current_time)
        if signal is not None:
            return signal

        return None
