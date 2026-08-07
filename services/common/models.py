from __future__ import annotations
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any

class ProtectionMode(str, Enum):
    FIXED_OCO = 'FIXED_OCO'
    TRAILING_ONLY = 'TRAILING_ONLY'
    OCO_TRAILING = 'OCO_TRAILING'

class ExecutionMode(str, Enum):
    SIMULATION = 'simulation'
    TESTNET = 'testnet'
    LIVE = 'live'

class LifecycleState(str, Enum):
    SIGNAL_APPROVED = 'SIGNAL_APPROVED'
    ENTRY_SUBMITTED = 'ENTRY_SUBMITTED'
    ENTRY_PARTIALLY_FILLED = 'ENTRY_PARTIALLY_FILLED'
    ENTRY_FILLED = 'ENTRY_FILLED'
    PROTECTION_ACTIVE = 'PROTECTION_ACTIVE'
    BREAK_EVEN_ARMED = 'BREAK_EVEN_ARMED'
    PROFIT_LOCKED = 'PROFIT_LOCKED'
    TRAILING_ACTIVE = 'TRAILING_ACTIVE'
    EXIT_FILLED = 'EXIT_FILLED'
    ENTRY_REJECTED = 'ENTRY_REJECTED'
    AMBIGUOUS = 'AMBIGUOUS'
    RECONCILED = 'RECONCILED'
    ERROR = 'ERROR'

@dataclass(frozen=True)
class Signal:
    signal_id: str
    pair: str
    symbol: str
    candle_time: str
    generated_at: str
    strategy: str
    entry_tag: str
    pair_state_hash: str
    payload: dict[str, Any]
    def to_dict(self):
        return asdict(self)
