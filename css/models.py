from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from models.pick import RawPick, MarketType


@dataclass
class ConsensusComponent:
    pick: RawPick
    weight: float


@dataclass
class ConsensusPick:
    fixture_key: str
    market: MarketType
    selection: str
    components: List[ConsensusComponent]
    consensus_score: float
    implied_odds: Optional[Decimal]
