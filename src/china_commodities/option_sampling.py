"""Select paired strikes with a fixed budget; assumptions are never market IV."""
import math
from statistics import NormalDist, median


def bounded_strikes(strikes, reference, years, budget, previous=()):
    strikes = sorted(set(strikes))
    if not strikes or budget < 1:
        return [], {}
    sigmas = [r['iv_percent']/100 for r in previous
              if isinstance(r.get('iv_percent'), (int, float))
              and not isinstance(r['iv_percent'], bool) and 0 < r['iv_percent'] <= 500]
    sigma = median(sigmas) if sigmas else .25
    scale = sigma * math.sqrt(max(years, 1/365))
    normal = NormalDist()
    selected = [min(strikes, key=lambda k: (abs(k-reference), k))]
    wings = []
    # Prior delta ages as spot and T move. Reproject all directory strikes using
    # today's reference and prior sample IV; never blindly reuse yesterday's delta.
    for side, target in [('P', -.25), ('C', .25), ('P', -.10), ('C', .10)]:
        candidates = [k for k in strikes if k not in selected
                      and (k < reference if side == 'P' else k > reference)]
        if len(selected) >= budget or not candidates:
            wings.append(dict(side=side, target_delta=target, strike=None, state='budget_or_directory_limit'))
            continue
        def delta(k):
            call = normal.cdf((math.log(reference/k) + .5*scale*scale)/scale)
            return call if side == 'C' else call-1
        k = min(candidates, key=lambda k: (abs(delta(k)-target), abs(k-reference), k))
        selected.append(k)
        wings.append(dict(side=side, target_delta=target, strike=k,
                          selection_delta_proxy=delta(k), state='selected_proxy_not_observed_delta'))
    oi = {}
    for row in previous:
        k, value = row.get('strike'), row.get('open_interest')
        if k in strikes and isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            oi[k] = oi.get(k, 0) + value
    for k in sorted(oi, key=lambda k: (-oi[k], abs(k-reference), k))[:2] + sorted(strikes, key=lambda k: (abs(k-reference), k)):
        if len(selected) >= budget:
            break
        if k not in selected:
            selected.append(k)
    return sorted(selected), dict(selection_sigma=sigma,
        sigma_source='previous_sample_median_IV' if sigmas else 'fixed_25pct_selection_assumption',
        wing_selection=wings, selection_method='reprojected_reference_and_IV_proxy_with_distinct_wings')
