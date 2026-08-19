"""Consistent end-of-day Greeks for options on commodity futures.

The module deliberately requires an explicit exercise style.  A Chinese
commodity option must not silently fall back to Black-76 when the contract is
American-style.  European options use Black-76; American options use a CRR
tree where the futures price is a risk-neutral martingale.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Literal


OptionType = Literal["C", "P"]
ExerciseStyle = Literal["european", "american"]


@dataclass(frozen=True)
class OptionValuationInput:
    forward: float
    strike: float
    time_to_expiry_years: float
    rate: float
    option_type: OptionType
    exercise_style: ExerciseStyle
    market_price: float | None = None
    iv_percent: float | None = None


@dataclass(frozen=True)
class GreeksResult:
    model: str
    iv_percent: float
    delta: float
    gamma: float
    vega_per_vol_point: float
    theta_per_calendar_day: float
    rho_per_rate_point: float
    iv_source: str
    quality: str

    def to_dict(self) -> dict[str, float | str]:
        return asdict(self)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _validate_common(
    forward: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    option_type: str,
) -> OptionType:
    normalized_type = str(option_type).upper()
    if normalized_type not in {"C", "P"}:
        raise ValueError("option_type must be C or P")
    if forward <= 0 or strike <= 0:
        raise ValueError("forward and strike must be positive")
    if time_to_expiry_years <= 0:
        raise ValueError("time_to_expiry_years must be positive")
    if volatility <= 0:
        raise ValueError("volatility must be positive")
    return normalized_type  # type: ignore[return-value]


def black76_price(
    forward: float,
    strike: float,
    time_to_expiry_years: float,
    rate: float,
    volatility: float,
    option_type: str,
) -> float:
    """Return a European option-on-futures price under Black-76."""
    normalized_type = _validate_common(
        forward, strike, time_to_expiry_years, volatility, option_type
    )
    root_time = math.sqrt(time_to_expiry_years)
    d1 = (
        math.log(forward / strike) + 0.5 * volatility * volatility * time_to_expiry_years
    ) / (volatility * root_time)
    d2 = d1 - volatility * root_time
    discount = math.exp(-rate * time_to_expiry_years)
    if normalized_type == "C":
        return discount * (
            forward * _normal_cdf(d1) - strike * _normal_cdf(d2)
        )
    return discount * (
        strike * _normal_cdf(-d2) - forward * _normal_cdf(-d1)
    )


def american_futures_option_price(
    forward: float,
    strike: float,
    time_to_expiry_years: float,
    rate: float,
    volatility: float,
    option_type: str,
    *,
    steps: int = 100,
) -> float:
    """Price an American option on futures with a CRR recombining tree."""
    normalized_type = _validate_common(
        forward, strike, time_to_expiry_years, volatility, option_type
    )
    if steps < 10:
        raise ValueError("steps must be at least 10")
    dt = time_to_expiry_years / steps
    up = math.exp(volatility * math.sqrt(dt))
    down = 1.0 / up
    probability = (1.0 - down) / (up - down)
    probability = min(1.0, max(0.0, probability))
    discount = math.exp(-rate * dt)

    def payoff(value: float) -> float:
        return max(value - strike, 0.0) if normalized_type == "C" else max(
            strike - value, 0.0
        )

    values = [
        payoff(forward * (up ** node) * (down ** (steps - node)))
        for node in range(steps + 1)
    ]
    for level in range(steps - 1, -1, -1):
        next_values: list[float] = []
        for node in range(level + 1):
            continuation = discount * (
                probability * values[node + 1]
                + (1.0 - probability) * values[node]
            )
            node_forward = forward * (up ** node) * (down ** (level - node))
            next_values.append(max(continuation, payoff(node_forward)))
        values = next_values
    return values[0]


def option_price(
    forward: float,
    strike: float,
    time_to_expiry_years: float,
    rate: float,
    volatility: float,
    option_type: str,
    exercise_style: str,
    *,
    tree_steps: int = 100,
) -> float:
    style = str(exercise_style).lower()
    if style == "european":
        return black76_price(
            forward,
            strike,
            time_to_expiry_years,
            rate,
            volatility,
            option_type,
        )
    if style == "american":
        return american_futures_option_price(
            forward,
            strike,
            time_to_expiry_years,
            rate,
            volatility,
            option_type,
            steps=tree_steps,
        )
    raise ValueError("exercise_style must be explicitly european or american")


def implied_volatility(
    market_price: float,
    *,
    forward: float,
    strike: float,
    time_to_expiry_years: float,
    rate: float,
    option_type: str,
    exercise_style: str,
    tree_steps: int = 80,
    maximum_volatility: float = 5.0,
) -> float | None:
    """Invert the selected model, returning annualized volatility as a decimal."""
    if market_price <= 0:
        return None
    low = 1e-6
    high = maximum_volatility

    def model(volatility: float) -> float:
        return option_price(
            forward,
            strike,
            time_to_expiry_years,
            rate,
            volatility,
            option_type,
            exercise_style,
            tree_steps=tree_steps,
        )

    low_price = model(low)
    high_price = model(high)
    tolerance = max(1e-8, abs(market_price) * 1e-8)
    if market_price < low_price - tolerance or market_price > high_price + tolerance:
        return None
    for _ in range(80):
        middle = 0.5 * (low + high)
        middle_price = model(middle)
        if abs(middle_price - market_price) <= tolerance:
            return middle
        if middle_price < market_price:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


def _finite_difference_risks(
    valuation: OptionValuationInput,
    volatility: float,
    *,
    tree_steps: int,
) -> tuple[float, float, float, float, float]:
    base_arguments = {
        "strike": valuation.strike,
        "time_to_expiry_years": valuation.time_to_expiry_years,
        "rate": valuation.rate,
        "volatility": volatility,
        "option_type": valuation.option_type,
        "exercise_style": valuation.exercise_style,
        "tree_steps": tree_steps,
    }
    forward_bump = max(valuation.forward * 0.001, 1e-4)
    price_up = option_price(
        forward=valuation.forward + forward_bump, **base_arguments
    )
    price_base = option_price(forward=valuation.forward, **base_arguments)
    price_down = option_price(
        forward=max(1e-8, valuation.forward - forward_bump), **base_arguments
    )
    delta = (price_up - price_down) / (2.0 * forward_bump)
    gamma = (price_up - 2.0 * price_base + price_down) / (forward_bump**2)

    half_vol_point = 0.005
    vol_down = max(1e-6, volatility - half_vol_point)
    vol_up = volatility + half_vol_point
    vega = option_price(
        forward=valuation.forward,
        **dict(base_arguments, volatility=vol_up),
    ) - option_price(
        forward=valuation.forward,
        **dict(base_arguments, volatility=vol_down),
    )

    one_day = 1.0 / 365.0
    shorter_time = max(1e-8, valuation.time_to_expiry_years - one_day)
    theta = option_price(
        forward=valuation.forward,
        **dict(base_arguments, time_to_expiry_years=shorter_time),
    ) - price_base

    half_rate_point = 0.005
    rho = option_price(
        forward=valuation.forward,
        **dict(base_arguments, rate=valuation.rate + half_rate_point),
    ) - option_price(
        forward=valuation.forward,
        **dict(base_arguments, rate=valuation.rate - half_rate_point),
    )
    return delta, gamma, vega, theta, rho


def calculate_greeks(
    valuation: OptionValuationInput,
    *,
    tree_steps: int = 100,
) -> GreeksResult | None:
    """Calculate a consistent EOD Greek set, or return ``None`` if IV is invalid."""
    style = str(valuation.exercise_style).lower()
    if style not in {"european", "american"}:
        raise ValueError("exercise_style must be explicitly european or american")
    if valuation.forward <= 0 or valuation.strike <= 0:
        return None
    if valuation.time_to_expiry_years <= 0:
        return None

    if valuation.iv_percent is not None and valuation.iv_percent > 0:
        volatility = valuation.iv_percent / 100.0
        iv_source = "vendor"
    elif valuation.market_price is not None:
        volatility = implied_volatility(
            valuation.market_price,
            forward=valuation.forward,
            strike=valuation.strike,
            time_to_expiry_years=valuation.time_to_expiry_years,
            rate=valuation.rate,
            option_type=valuation.option_type,
            exercise_style=style,
            tree_steps=min(tree_steps, 80),
        )
        if volatility is None:
            return None
        iv_source = "model_inverted"
    else:
        return None

    delta, gamma, vega, theta, rho = _finite_difference_risks(
        valuation, volatility, tree_steps=tree_steps
    )
    model = "black_76" if style == "european" else "crr_american_futures"
    return GreeksResult(
        model=model,
        iv_percent=volatility * 100.0,
        delta=delta,
        gamma=gamma,
        vega_per_vol_point=vega,
        theta_per_calendar_day=theta,
        rho_per_rate_point=rho,
        iv_source=iv_source,
        quality="model_consistent_eod",
    )
