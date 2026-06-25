from .config import SOURCE_WEIGHTS


def normalise_confidence(pick):
    if pick.source_slug == "forebet":
        return float(pick.confidence or 0.6)
    if pick.source_slug == "soccervista":
        return float(pick.confidence or 0.6)
    if pick.source_slug == "olbg":
        c = float(pick.confidence or 0)
        return max(0.4, min(0.85, c / 100.0))
    if pick.source_slug == "freesupertips":
        return float(pick.confidence or 0.65)
    return float(pick.confidence or 0.5)


def effective_weight(pick):
    base = SOURCE_WEIGHTS.get(pick.source_slug, 0.7)
    conf = normalise_confidence(pick)
    return base * conf
