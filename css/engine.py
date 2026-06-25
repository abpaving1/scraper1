from collections import defaultdict
from typing import Iterable, Dict, Tuple

from models.pick import RawPick, MarketType
from .models import ConsensusPick, ConsensusComponent
from .normalise import effective_weight


def fixture_key(pick: RawPick) -> str:
    return f"{pick.home_team_name} v {pick.away_team_name}"


def build_consensus(picks: Iterable[RawPick]) -> Dict[Tuple[str, MarketType], ConsensusPick]:
    groups = defaultdict(list)

    for p in picks:
        key = (fixture_key(p), p.market, p.selection)
        groups[key].append(p)

    consensus = {}

    for (fx_key, market, selection), group in groups.items():
        components = []
        total_weight = 0.0

        for p in group:
            w = effective_weight(p)
            components.append(ConsensusComponent(pick=p, weight=w))
            total_weight += w

        implied_odds = group[0].odds_decimal if group[0].odds_decimal else None

        consensus[(fx_key, market)] = ConsensusPick(
            fixture_key=fx_key,
            market=market,
            selection=selection,
            components=components,
            consensus_score=round(total_weight, 3),
            implied_odds=implied_odds,
        )

    return consensus


def rank_consensus_picks(consensus: Dict[Tuple[str, MarketType], ConsensusPick]):
    picks = list(consensus.values())
    picks.sort(key=lambda c: c.consensus_score, reverse=True)
    return picks
