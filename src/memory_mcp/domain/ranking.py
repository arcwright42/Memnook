from collections import defaultdict
from collections.abc import Sequence


def rrf(*rankings: Sequence[str], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        # Multiple mentions within one route must not multiply its contribution.
        for rank, identifier in enumerate(dict.fromkeys(ranking), start=1):
            scores[identifier] += 1 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
