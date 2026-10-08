"""Related-contingency pricing engine for horse racing (Harville model).

A "related contingency" here means a multiple/accumulator bet where the outcome
of one selection directly influences the outcome of another *within the same race*.
For example:

    - Horse A to win AND Horse B to finish 2nd
    - Horse A to win AND Horse B to be placed (top-N per each-way terms)
    - Horse A to win AND Horse B to NOT win

Because both legs live in the same race, the outcomes are NOT independent, so
you cannot simply multiply the two win prices. Instead we use conditional
probability:

    P(A and B) = P(A) * P(B | A)

The standard industry approximation for "who finishes where" derived from win
prices is the **Harville model**. Its core assumption: if you remove the winner
from the field, every remaining runner's chance of being "next" is just its win
probability re-normalised over the runners that are left.

    P(A wins)          = p_A
    P(B is 2nd | A win)= p_B / (1 - p_A)
    P(A wins & B 2nd)  = p_A * p_B / (1 - p_A)

IMPORTANT MODEL CAVEATS
-----------------------
- The Harville model treats finishing order as driven purely by win
  probabilities. It is the standard, defensible approximation but is known to
  slightly OVERSTATE the place chances of strong favourites in real racing.
  Treat outputs as model estimates, not ground truth.
- Input win prices carry the bookmaker's margin (overround). We strip that to a
  "fair book" before doing any conditional maths, otherwise the probabilities
  don't sum to 1 and the conditioning is distorted. The margin you WANT to apply
  to the final quote is a separate, explicit input (`apply_margin`).

All prices are decimal odds. All functions are pure (no API, no I/O).
"""

from __future__ import annotations

from dataclasses import dataclass


# ── Basic probability <-> price conversions ──


def implied_prob(decimal_price: float) -> float:
    """Convert a decimal price to its implied probability (includes margin).

    e.g. 4.0 -> 0.25
    Raises ValueError for non-positive prices.
    """
    if decimal_price is None or decimal_price <= 0:
        raise ValueError(f"decimal price must be > 0, got {decimal_price!r}")
    return 1.0 / decimal_price


def prob_to_price(prob: float) -> float:
    """Convert a probability to a fair decimal price.

    e.g. 0.25 -> 4.0
    Raises ValueError for probabilities outside (0, 1].
    """
    if prob <= 0 or prob > 1:
        raise ValueError(f"probability must be in (0, 1], got {prob!r}")
    return 1.0 / prob


# ── Overround handling ──


def book_overround(prices: list[float]) -> float:
    """Total book percentage for a list of win prices.

    A fair book sums to 1.0 (100%). A real book sums to > 1.0; the excess is the
    bookmaker's margin. e.g. a book summing to 1.15 has a 15% overround.
    """
    return sum(implied_prob(p) for p in prices)


def fair_probabilities(prices: list[float]) -> list[float]:
    """Strip the bookmaker margin from win prices, returning true probabilities.

    Uses proportional (basic) normalisation: each raw implied probability is
    divided by the book total so the result sums to exactly 1.0. This is the
    simplest and most common de-margining method.

    Returns a list aligned to the input `prices`.
    """
    raw = [implied_prob(p) for p in prices]
    total = sum(raw)
    if total <= 0:
        raise ValueError("cannot normalise: probabilities sum to zero")
    return [r / total for r in raw]


# ── Result container ──


@dataclass
class ContingencyPrice:
    """The output of pricing a related contingency.

    fair_prob   — model probability of the combined outcome (margin-free)
    fair_price  — decimal price implied by fair_prob (the "true" price)
    quote_price — fair_price with your margin applied (the price you'd offer)
    margin_pct  — the margin applied to derive quote_price
    label       — human-readable description of the contingency
    """
    fair_prob: float
    fair_price: float
    quote_price: float
    margin_pct: float
    label: str

    def describe(self) -> str:
        return (
            f"{self.label}\n"
            f"  Fair probability: {self.fair_prob * 100:.2f}%\n"
            f"  Fair price:       {self.fair_price:.2f}\n"
            f"  Quote price:      {self.quote_price:.2f}  (margin {self.margin_pct:.1f}%)"
        )


def apply_margin(fair_price: float, margin_pct: float) -> float:
    """Apply a margin to a fair price to get the price you'd actually quote.

    A positive margin shortens the offered price (worse for the backer, the
    bookmaker's edge). margin_pct is expressed in percent, e.g. 10.0 for 10%.

    We apply the margin on the probability side: the quoted probability is the
    fair probability scaled up by (1 + margin), then converted back to a price.
    This keeps margin semantics consistent with book overround.
    """
    if fair_price <= 0:
        raise ValueError(f"fair_price must be > 0, got {fair_price!r}")
    fair_p = 1.0 / fair_price
    quoted_p = fair_p * (1.0 + margin_pct / 100.0)
    if quoted_p >= 1.0:
        # Contingency is near-certain after margin; clamp just below evens-on limit.
        quoted_p = min(quoted_p, 0.999999)
    return 1.0 / quoted_p


# ── Harville conditional probabilities ──
#
# All of the following take `fair_probs` — a list of MARGIN-FREE win
# probabilities that sum to 1.0 (use fair_probabilities() to produce them) — and
# integer indices into that list identifying the runners involved.


def p_a_wins(fair_probs: list[float], a: int) -> float:
    """P(runner A wins) = its fair win probability."""
    return fair_probs[a]


def p_a_wins_b_second(fair_probs: list[float], a: int, b: int) -> float:
    """P(A wins AND B finishes 2nd), Harville model.

        = p_A * p_B / (1 - p_A)

    The (1 - p_A) term re-normalises B's chance over the field with A removed.
    """
    if a == b:
        return 0.0  # a single horse cannot be both 1st and 2nd
    pa = fair_probs[a]
    pb = fair_probs[b]
    denom = 1.0 - pa
    if denom <= 0:
        return 0.0
    return pa * (pb / denom)


def p_b_placed_given_a_wins(fair_probs: list[float], a: int, b: int, places: int) -> float:
    """P(B finishes in the top `places` | A wins), Harville model.

    With A removed from the field, B must occupy one of the remaining
    (places - 1) podium spots (position 1 is taken by A). We sum the
    probabilities of B being 2nd, 3rd, ... up to `places`, each computed by
    sequentially removing the higher-placed runners.

    Because we condition on A having won, and we only care about whether B lands
    in ANY of the remaining place slots (not the exact slot), we can compute this
    as: the probability that B is among the top (places-1) of the field-minus-A,
    under the Harville sequential-removal model. We approximate the "B is in the
    next k finishers" probability by summing exact-position probabilities.
    """
    if places < 1:
        raise ValueError("places must be >= 1")
    if a == b:
        return 0.0
    pa = fair_probs[a]
    denom = 1.0 - pa
    if denom <= 0:
        return 0.0

    # Remaining field probabilities after removing A, re-normalised.
    n = len(fair_probs)
    remaining = {i: fair_probs[i] / denom for i in range(n) if i != a}

    # B must be in one of positions 2..places overall => positions 1..(places-1)
    # within the remaining field.
    slots = places - 1
    if slots <= 0:
        return 0.0

    return _prob_in_top_k(remaining, b, slots)


def _prob_in_top_k(probs: dict[int, float], target: int, k: int) -> float:
    """P(target is among the first k finishers) for a field given by `probs`.

    `probs` maps runner index -> probability, summing to ~1.0. Uses the Harville
    sequential model: P(finishes 1st) = p; P(finishes 2nd) = sum over other
    runners r of P(r 1st) * p/(1 - p_r); etc. We accumulate the probability that
    `target` lands in any of the first k positions.

    Implemented via recursion over which runners have been "removed" so far.
    k is small (place counts are typically 2-4) and fields are modest, but to
    keep this tractable we cap the exact recursion and fall back on the dominant
    terms. For standard place counts this computes the exact Harville value.
    """
    if k <= 0:
        return 0.0
    if target not in probs:
        return 0.0

    # Position 1: target finishes first.
    total = probs[target]
    if k == 1:
        return total

    # For positions 2..k, condition on some other runner taking the earlier slot.
    # We enumerate ordered prefixes of length up to (k-1) that exclude `target`,
    # then add the probability `target` is next.
    def recurse(remaining: dict[int, float], depth: int) -> float:
        # depth = number of positions already filled by non-target runners
        # We want target to finish at position depth+1 (<= k).
        norm = sum(remaining.values())
        if norm <= 0:
            return 0.0
        # Probability target is the very next finisher.
        p_target_next = remaining.get(target, 0.0) / norm
        acc = p_target_next
        # If we still have slots left, let another runner take the next spot.
        if depth + 1 < k:
            for r, pr in list(remaining.items()):
                if r == target:
                    continue
                p_r_next = pr / norm
                sub = {i: v for i, v in remaining.items() if i != r}
                acc += p_r_next * recurse(sub, depth + 1)
        return acc

    # Position 1 already counted above (target first). Now handle target at
    # positions 2..k by first removing one non-target runner for slot 1.
    norm_all = sum(probs.values())
    positions_2_to_k = 0.0
    for r, pr in list(probs.items()):
        if r == target:
            continue
        p_r_first = pr / norm_all
        sub = {i: v for i, v in probs.items() if i != r}
        positions_2_to_k += p_r_first * recurse(sub, 1)

    return total + positions_2_to_k


def p_a_wins_b_not_win(fair_probs: list[float], a: int, b: int) -> float:
    """P(A wins AND B does NOT win).

    If A and B are different runners, then A winning already guarantees B did not
    win (only one winner). So this is simply P(A wins).

    If a == b it is a contradiction (a horse cannot both win and not win) -> 0.
    """
    if a == b:
        return 0.0
    return fair_probs[a]


# ── Top-level contingency pricers (produce a ContingencyPrice) ──


def price_win_and_second(
    prices: list[float],
    a: int,
    b: int,
    margin_pct: float = 0.0,
    labels: list[str] | None = None,
) -> ContingencyPrice:
    """Price 'A wins AND B finishes 2nd' from a race's win prices."""
    fair = fair_probabilities(prices)
    prob = p_a_wins_b_second(fair, a, b)
    return _finish(prob, margin_pct, _label("wins", "finishes 2nd", a, b, labels))


def price_win_and_placed(
    prices: list[float],
    a: int,
    b: int,
    places: int,
    margin_pct: float = 0.0,
    labels: list[str] | None = None,
) -> ContingencyPrice:
    """Price 'A wins AND B is placed (top-`places`)' from a race's win prices."""
    fair = fair_probabilities(prices)
    p_b = p_b_placed_given_a_wins(fair, a, b, places)
    prob = fair[a] * p_b
    return _finish(prob, margin_pct, _label("wins", f"is placed (top {places})", a, b, labels))


def price_win_and_not_win(
    prices: list[float],
    a: int,
    b: int,
    margin_pct: float = 0.0,
    labels: list[str] | None = None,
) -> ContingencyPrice:
    """Price 'A wins AND B does NOT win' from a race's win prices."""
    fair = fair_probabilities(prices)
    prob = p_a_wins_b_not_win(fair, a, b)
    return _finish(prob, margin_pct, _label("wins", "does not win", a, b, labels))


# ── Internal helpers ──


def _finish(prob: float, margin_pct: float, label: str) -> ContingencyPrice:
    if prob <= 0:
        # Impossible / contradictory contingency.
        return ContingencyPrice(
            fair_prob=0.0,
            fair_price=float("inf"),
            quote_price=float("inf"),
            margin_pct=margin_pct,
            label=label,
        )
    fair_price = prob_to_price(prob)
    quote = apply_margin(fair_price, margin_pct)
    return ContingencyPrice(
        fair_prob=prob,
        fair_price=fair_price,
        quote_price=quote,
        margin_pct=margin_pct,
        label=label,
    )


def _label(a_verb: str, b_verb: str, a: int, b: int, labels: list[str] | None) -> str:
    a_name = labels[a] if labels and a < len(labels) else f"Runner {a}"
    b_name = labels[b] if labels and b < len(labels) else f"Runner {b}"
    return f"{a_name} {a_verb} AND {b_name} {b_verb}"


# ── Independent (cross-race) doubles ──
#
# For legs in DIFFERENT races (e.g. a horse to win the 16:30 today AND to win an
# ante-post feature race later), the outcomes are essentially independent, so the
# Harville same-race maths above does NOT apply. The fair combined probability is
# the product of the two fair leg probabilities:
#
#     P(A and B) = P(A) * P(B)          (independent)
#
# with an OPTIONAL correlation adjustment for the mild real-world dependence when
# it's the same horse in both races (a win in leg 1 can shorten leg 2).


def _fair_leg_prob(decimal_price: float, leg_margin_pct: float) -> float:
    """Strip a per-leg margin from a single price to estimate its true probability.

    A lone win price carries the bookmaker's margin but we have no field to
    normalise against, so the caller states how much margin that individual price
    is assumed to contain. leg_margin_pct = 0 treats the quoted price as already
    fair.

        fair_prob = implied_prob(price) / (1 + leg_margin_pct/100)
    """
    raw = implied_prob(decimal_price)
    fair = raw / (1.0 + leg_margin_pct / 100.0)
    if fair <= 0 or fair >= 1:
        raise ValueError(
            f"de-margined leg probability out of range ({fair:.4f}); "
            f"check price {decimal_price} and leg margin {leg_margin_pct}%"
        )
    return fair


def price_independent_double(
    price_a: float,
    price_b: float,
    margin_pct: float = 0.0,
    leg_a_margin_pct: float = 0.0,
    leg_b_margin_pct: float = 0.0,
    correlation: float = 0.0,
    labels: tuple[str, str] | None = None,
) -> ContingencyPrice:
    """Price a double whose two legs are in DIFFERENT (independent) races.

    Args:
        price_a, price_b: decimal win prices for each leg.
        margin_pct: margin to apply to the FINAL double quote.
        leg_a_margin_pct, leg_b_margin_pct: assumed margin already inside each
            individual leg price, stripped before combining. Default 0 = treat
            the quoted prices as already fair.
        correlation: optional adjustment in [-1, 1] for same-horse dependence.
            0  = pure independence (fair_prob = p_a * p_b) — the standard default.
            >0 = outcomes reinforce (leg-2 more likely given leg-1 won) -> shorter
                 double. <0 = they offset -> longer double. The adjustment scales
                 the joint probability toward the "fully dependent" bound
                 min(p_a, p_b) for positive correlation, or toward the
                 independent-minus bound for negative.
        labels: optional (name_a, name_b) for the description.

    Returns a ContingencyPrice.
    """
    if not -1.0 <= correlation <= 1.0:
        raise ValueError(f"correlation must be in [-1, 1], got {correlation}")

    p_a = _fair_leg_prob(price_a, leg_a_margin_pct)
    p_b = _fair_leg_prob(price_b, leg_b_margin_pct)

    independent = p_a * p_b

    if correlation > 0:
        # Interpolate toward the upper (fully dependent) bound min(p_a, p_b).
        upper = min(p_a, p_b)
        joint = independent + correlation * (upper - independent)
    elif correlation < 0:
        # Interpolate toward the lower bound max(0, p_a + p_b - 1).
        lower = max(0.0, p_a + p_b - 1.0)
        joint = independent + abs(correlation) * (lower - independent)
    else:
        joint = independent

    name_a = labels[0] if labels else "Leg A"
    name_b = labels[1] if labels else "Leg B"
    corr_note = "" if correlation == 0 else f" [corr {correlation:+.2f}]"
    label = f"{name_a} AND {name_b}{corr_note}"

    return _finish(joint, margin_pct, label)
