"""Discrete heavy-tail fitting for avalanche sizes (primary scientific estimator).

Methods follow Clauset, Shalizi & Newman (2009) and, for the truncated model,
Klaus, Yu & Plenz (2011) / Corral & Deluca (2013):

* pure discrete power law ``p(s) = s^-g / zeta(g, s_min)`` (Hurwitz zeta),
  optionally bounded ``[s_min, s_max]``; MLE by bounded 1-D optimisation;
* ``s_min`` chosen by minimising the KS distance between the empirical and
  fitted CDFs over all candidate ``s_min`` meeting ``min_tail`` and
  ``min_distinct`` (the whole candidate table is returned);
* gamma CI: nonparametric bootstrap of the tail with ``s_min`` fixed
  (percentile interval);
* goodness of fit: semi-parametric bootstrap (Clauset eq. 4.x): synthetic data
  sets drawn from the fitted tail with probability ``n_tail/n`` and resampled
  from the empirical body otherwise; the *entire* procedure (``s_min`` scan +
  MLE) is refitted; ``p = P(KS_syn >= KS_obs)``;
* truncated power law ``p(s) ∝ s^-g exp(-s/s_c)`` (MLE over ``g, log(1/s_c)``,
  normalisation by explicit summation plus a bounded tail term);
* alternatives: discrete exponential, discretised lognormal;
* comparisons: Vuong normalised log-likelihood ratio for non-nested pairs;
  one-sided boundary LR test (``0.5 * chi2_1``) for pure vs truncated.

Log-log OLS is NOT used here; see :mod:`snnrc.criticality.legacy_ols`.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
from scipy import optimize
from scipy.special import erfc, exp1, gammaincc, log_ndtr, zeta
from scipy.special import gamma as gamma_fn
from scipy.stats import chi2

GAMMA_BOUNDS = (1.0 + 1e-6, 8.0)
SAMPLE_CAP = 10**15   # upper cap for synthetic samples (only reached when gamma ~ 1)


# --------------------------------------------------------------------------- utilities
def _tail(x, xmin: int, xmax: Optional[int] = None) -> np.ndarray:
    x = np.asarray(x, dtype=np.int64)
    m = x >= xmin
    if xmax is not None:
        m &= x <= xmax
    return x[m]


def _log_norm_pl(g: float, xmin: int, xmax: Optional[int] = None) -> float:
    z = zeta(g, xmin)
    if xmax is not None:
        z -= zeta(g, xmax + 1)
    return float(np.log(z))


# --------------------------------------------------------------------------- pure power law
def pl_pointwise_loglik(tail: np.ndarray, g: float, xmin: int, xmax: Optional[int] = None) -> np.ndarray:
    return -g * np.log(tail) - _log_norm_pl(g, xmin, xmax)


def fit_power_law(x, xmin: int, xmax: Optional[int] = None) -> Dict:
    tail = _tail(x, xmin, xmax)
    n = len(tail)
    if n == 0:
        raise ValueError("empty tail")
    S = float(np.log(tail).sum())
    lo = 1e-6 if xmax is not None else GAMMA_BOUNDS[0]

    def nll(g):
        return g * S + n * _log_norm_pl(g, xmin, xmax)

    r = optimize.minimize_scalar(nll, bounds=(lo, GAMMA_BOUNDS[1]), method="bounded", options={"xatol": 1e-7})
    return {"gamma": float(r.x), "loglik": float(-r.fun), "n_tail": n, "xmin": int(xmin),
            "xmax": None if xmax is None else int(xmax)}


def pl_cdf(values: np.ndarray, g: float, xmin: int, xmax: Optional[int] = None) -> np.ndarray:
    Z = np.exp(_log_norm_pl(g, xmin, xmax))
    upper = zeta(g, xmax + 1) if xmax is not None else 0.0
    return 1.0 - (zeta(g, np.asarray(values, float) + 1) - upper) / Z


def ks_distance_power_law(x, g: float, xmin: int, xmax: Optional[int] = None) -> float:
    tail = _tail(x, xmin, xmax)
    u, c = np.unique(tail, return_counts=True)
    emp = np.cumsum(c) / c.sum()
    return float(np.max(np.abs(emp - pl_cdf(u, g, xmin, xmax))))


def scan_xmin(x, min_tail: int = 50, min_distinct: int = 10, max_candidates: Optional[int] = None,
              xmax: Optional[int] = None) -> Dict:
    x = np.asarray(x, dtype=np.int64)
    x = x[x > 0]
    u = np.unique(x)
    sx = np.sort(x)
    cands = []
    for i, v in enumerate(u):
        n_tail = len(sx) - np.searchsorted(sx, v, side="left")
        if n_tail < min_tail or len(u) - i < min_distinct:
            break
        cands.append(int(v))
    if max_candidates is not None and len(cands) > max_candidates:
        idx = np.unique(np.round(np.linspace(0, len(cands) - 1, max_candidates)).astype(int))
        cands = [cands[i] for i in idx]
    table: List[Dict] = []
    for v in cands:
        f = fit_power_law(x, v, xmax)
        tail = _tail(x, v, xmax)
        table.append({"xmin": v, "gamma": f["gamma"], "ks": ks_distance_power_law(x, f["gamma"], v, xmax),
                      "n_tail": f["n_tail"], "n_distinct": int(len(np.unique(tail))), "loglik": f["loglik"]})
    best = min(table, key=lambda r: r["ks"]) if table else None
    return {"table": table, "best": best, "min_tail": min_tail, "min_distinct": min_distinct,
            "max_candidates": max_candidates}


# --------------------------------------------------------------------------- sampling
def sample_discrete_power_law(n: int, gamma: float, xmin: int, rng: np.random.Generator,
                              table_size: int = 200_000) -> np.ndarray:
    """Exact inverse-CDF sampling on ``[xmin, xmin+table_size)``; the remaining
    (tiny) mass uses the continuous approximation of Clauset et al. (2009, App. D)."""

    s = np.arange(xmin, xmin + table_size, dtype=np.float64)
    pmf = s ** (-gamma) / zeta(gamma, xmin)
    cdf = np.cumsum(pmf)
    u = rng.random(n)
    idx = np.searchsorted(cdf, u, side="right")
    out = (xmin + idx).astype(np.int64)
    beyond = idx >= table_size
    if beyond.any():
        top = xmin + table_size
        v = (u[beyond] - cdf[-1]) / max(1.0 - cdf[-1], 1e-300)
        v = np.clip(v, 0.0, 1.0 - 1e-16)
        with np.errstate(over="ignore"):
            tail_vals = np.floor((top - 0.5) * (1.0 - v) ** (-1.0 / (gamma - 1.0)) + 0.5)
        # gamma -> 1 makes the tail astronomically heavy; cap (not wrap) at SAMPLE_CAP
        out[beyond] = np.minimum(np.nan_to_num(tail_vals, nan=SAMPLE_CAP, posinf=SAMPLE_CAP), SAMPLE_CAP).astype(np.int64)
    return out


def sample_truncated_power_law(n: int, gamma: float, s_c: float, xmin: int, rng: np.random.Generator) -> np.ndarray:
    top = int(xmin + max(60 * s_c, 1000))
    s = np.arange(xmin, top, dtype=np.float64)
    w = np.exp(-gamma * np.log(s) - s / s_c)
    cdf = np.cumsum(w / w.sum())
    idx = np.minimum(np.searchsorted(cdf, rng.random(n), side="right"), len(s) - 1)
    return (xmin + idx).astype(np.int64)


# --------------------------------------------------------------------------- bootstrap
def bootstrap_gamma_ci(x, xmin: int, n_boot: int, seed: int, ci: float = 0.95) -> Dict:
    rng = np.random.default_rng(seed)
    tail = _tail(x, xmin)
    gs = [fit_power_law(rng.choice(tail, size=len(tail), replace=True), xmin)["gamma"] for _ in range(n_boot)]
    lo, hi = np.percentile(gs, [50 * (1 - ci), 100 - 50 * (1 - ci)])
    return {"ci_low": float(lo), "ci_high": float(hi), "std": float(np.std(gs, ddof=1)) if n_boot > 1 else None,
            "n_boot": int(n_boot), "seed": int(seed), "level": ci, "method": "percentile_bootstrap_xmin_fixed"}


def gof_pvalue(x, n_boot: int, seed: int, min_tail: int = 50, min_distinct: int = 10,
               max_candidates: Optional[int] = None) -> Dict:
    x = np.asarray(x, dtype=np.int64)
    x = x[x > 0]
    obs = scan_xmin(x, min_tail, min_distinct, max_candidates)
    if obs["best"] is None:
        return {"p_value": None, "status": "insufficient_data", "n_boot": n_boot, "seed": seed}
    xmin, g, ks_obs = obs["best"]["xmin"], obs["best"]["gamma"], obs["best"]["ks"]
    body = x[x < xmin]
    p_tail = obs["best"]["n_tail"] / len(x)
    rng = np.random.default_rng(seed)
    ks_syn, failed = [], 0
    for _ in range(n_boot):
        n_t = rng.binomial(len(x), p_tail) if len(body) else len(x)
        syn = np.r_[sample_discrete_power_law(n_t, g, xmin, rng),
                    rng.choice(body, size=len(x) - n_t, replace=True) if len(body) else np.zeros(0, int)]
        r = scan_xmin(syn, min_tail, min_distinct, max_candidates)
        if r["best"] is None:
            failed += 1
            continue
        ks_syn.append(r["best"]["ks"])
    ks_syn = np.asarray(ks_syn)
    p = float(np.mean(ks_syn >= ks_obs)) if len(ks_syn) else None
    return {"p_value": p, "ks_observed": ks_obs, "n_boot": int(n_boot), "n_failed": failed, "seed": int(seed),
            "xmin": xmin, "gamma": g, "method": "semi_parametric_bootstrap_full_refit", "status": "ok"}


# --------------------------------------------------------------------------- truncated power law
_EXPLICIT_TERMS = 10_000


def _upper_gamma(a: float, x: float) -> float:
    """Upper incomplete gamma Gamma(a, x) for any real a and x > 0 (downward recurrence)."""

    if a > 0:
        return float(gamma_fn(a) * gammaincc(a, x))
    if a == 0:
        return float(exp1(x))
    return (_upper_gamma(a + 1.0, x) - x ** a * np.exp(-x)) / a


def _log_norm_trunc(g: float, lam: float, xmin: int) -> float:
    """log sum_{s >= xmin} s^-g exp(-lam s).

    Explicit sum over ``_EXPLICIT_TERMS`` terms, then an Euler-Maclaurin tail
    ``int_top^inf f + f(top)/2 - f'(top)/12`` with the integral
    ``lam^(g-1) Gamma(1-g, lam*top)`` (or ``top^(1-g)/(g-1)`` at lam = 0).
    """

    s = np.arange(xmin, xmin + _EXPLICIT_TERMS, dtype=np.float64)
    logw = -g * np.log(s) - lam * s
    m = logw.max()
    head = np.exp(logw - m).sum()
    top = float(xmin + _EXPLICIT_TERMS)
    f_top = top ** (-g) * np.exp(-lam * top)
    if lam > 0:
        integral = lam ** (g - 1.0) * _upper_gamma(1.0 - g, lam * top)
    elif g > 1:
        integral = top ** (1.0 - g) / (g - 1.0)
    else:
        return float("inf")
    tail = integral + 0.5 * f_top + f_top * (g / top + lam) / 12.0
    return float(m + np.log(head + tail * np.exp(-m)))


def trunc_pointwise_loglik(tail: np.ndarray, g: float, s_c: float, xmin: int) -> np.ndarray:
    lam = 1.0 / s_c
    return -g * np.log(tail) - lam * tail - _log_norm_trunc(g, lam, xmin)


def fit_truncated_power_law(x, xmin: int) -> Dict:
    tail = _tail(x, xmin).astype(np.float64)
    n = len(tail)
    S, Sx = np.log(tail).sum(), tail.sum()

    def nll(p):
        g, loglam = p
        if not (0.0 < g < GAMMA_BOUNDS[1]):
            return np.inf
        lam = np.exp(loglam)
        return g * S + lam * Sx + n * _log_norm_trunc(g, lam, xmin)

    pl = fit_power_law(tail.astype(np.int64), xmin)
    best = None
    for g0 in (pl["gamma"], max(pl["gamma"] - 0.5, 0.2), 1.2):
        for sc0 in (tail.mean(), tail.max(), 10 * tail.max()):
            r = optimize.minimize(nll, x0=[g0, -np.log(sc0)], method="Nelder-Mead",
                                  options={"xatol": 1e-6, "fatol": 1e-8, "maxiter": 4000})
            if best is None or r.fun < best.fun:
                best = r
    g, lam = float(best.x[0]), float(np.exp(best.x[1]))
    ll = float(-best.fun)
    if pl["loglik"] > ll:                         # boundary lambda -> 0 (pure power law) is better
        g, lam, ll = pl["gamma"], 0.0, pl["loglik"]
    return {"gamma": g, "s_c": (1.0 / lam) if lam > 0 else float("inf"), "lambda": lam, "loglik": ll,
            "n_tail": n, "xmin": int(xmin)}


# --------------------------------------------------------------------------- alternatives
def fit_exponential(x, xmin: int) -> Dict:
    tail = _tail(x, xmin).astype(np.float64)
    m = (tail - xmin).mean()
    lam = np.log1p(1.0 / m) if m > 0 else 50.0
    ll = exp_pointwise_loglik(tail, lam, xmin).sum()
    return {"lambda": float(lam), "loglik": float(ll), "n_tail": len(tail), "xmin": int(xmin)}


def exp_pointwise_loglik(tail, lam: float, xmin: int) -> np.ndarray:
    return np.log(-np.expm1(-lam)) - lam * (np.asarray(tail, float) - xmin)


def _log_phi_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """log(Phi(b) - Phi(a)) for b > a, numerically stable in both tails."""

    a = np.asarray(a, float); b = np.asarray(b, float)
    out = np.empty(np.broadcast(a, b).shape)
    upper = a > 0
    la, lb = log_ndtr(-a), log_ndtr(-b)            # survival logs
    with np.errstate(divide="ignore", invalid="ignore"):
        out[upper] = (la + np.log1p(-np.exp(lb - la)))[upper]
        ca, cb = log_ndtr(a), log_ndtr(b)
        out[~upper] = (cb + np.log1p(-np.exp(ca - cb)))[~upper]
    return out


def ln_pointwise_loglik(tail, mu: float, sigma: float, xmin: int) -> np.ndarray:
    t = np.asarray(tail, float)
    z = lambda y: (np.log(y) - mu) / sigma  # noqa: E731
    num = _log_phi_diff(z(t - 0.5), z(t + 0.5))
    den = log_ndtr(-z(np.array([xmin - 0.5])))[0]
    return num - den


def fit_lognormal(x, xmin: int) -> Dict:
    tail = _tail(x, xmin).astype(np.float64)
    lt = np.log(tail)

    def nll(p):
        mu, ls = p
        v = -ln_pointwise_loglik(tail, mu, np.exp(ls), xmin).sum()
        return v if np.isfinite(v) else 1e300

    best = None
    for mu0 in (lt.mean(), lt.mean() - 2 * lt.std(), 0.0):
        r = optimize.minimize(nll, x0=[mu0, np.log(max(lt.std(), 0.1))], method="Nelder-Mead",
                              options={"xatol": 1e-6, "fatol": 1e-8, "maxiter": 4000})
        if best is None or r.fun < best.fun:
            best = r
    return {"mu": float(best.x[0]), "sigma": float(np.exp(best.x[1])), "loglik": float(-best.fun),
            "n_tail": len(tail), "xmin": int(xmin)}


# --------------------------------------------------------------------------- comparisons
def _pointwise(x, xmin: int, model: str):
    tail = _tail(x, xmin)
    if model == "power_law":
        f = fit_power_law(x, xmin); return pl_pointwise_loglik(tail, f["gamma"], xmin), f
    if model == "truncated_power_law":
        f = fit_truncated_power_law(x, xmin)
        if f["lambda"] == 0:
            return pl_pointwise_loglik(tail, f["gamma"], xmin), f
        return trunc_pointwise_loglik(tail, f["gamma"], f["s_c"], xmin), f
    if model == "exponential":
        f = fit_exponential(x, xmin); return exp_pointwise_loglik(tail, f["lambda"], xmin), f
    if model == "lognormal":
        f = fit_lognormal(x, xmin); return ln_pointwise_loglik(tail, f["mu"], f["sigma"], xmin), f
    raise ValueError(model)


def compare_models(x, xmin: int, m1: str, m2: str) -> Dict:
    """Vuong test: R > 0 favours m1; ``p_value`` = significance of the sign of R."""

    l1, _ = _pointwise(x, xmin, m1)
    l2, _ = _pointwise(x, xmin, m2)
    d = l1 - l2
    n = len(d)
    R = float(d.sum())
    sd = float(d.std())
    p = float(erfc(abs(R) / (np.sqrt(2 * n) * sd))) if sd > 0 else 1.0
    return {"model_1": m1, "model_2": m2, "R": R, "normalized_R": R / (np.sqrt(n) * sd) if sd > 0 else 0.0,
            "p_value": p, "preferred": (m1 if R > 0 else m2) if p < 0.05 else "inconclusive",
            "test": "Vuong_normalized_LLR"}


def compare_power_law_vs_truncated(x, xmin: int) -> Dict:
    pl = fit_power_law(x, xmin)
    tr = fit_truncated_power_law(x, xmin)
    LR = max(0.0, 2.0 * (tr["loglik"] - pl["loglik"]))
    p = float(0.5 * chi2.sf(LR, 1)) if LR > 0 else 1.0
    return {"model_1": "power_law", "model_2": "truncated_power_law", "R": pl["loglik"] - tr["loglik"],
            "LR_statistic": LR, "p_value": p,
            "preferred": "truncated_power_law" if p < 0.05 else "power_law_not_rejected",
            "test": "nested_LR_boundary_0.5chi2_1", "truncated_fit": tr}


# --------------------------------------------------------------------------- full protocol
def fit_avalanche_sizes(sizes, n_boot_ci: int = 200, n_boot_gof: int = 200, seed: int = 0,
                        min_tail: int = 50, min_distinct: int = 10, max_candidates: Optional[int] = 40,
                        min_decades: float = 1.0) -> Dict:
    from .legacy_ols import gamma_ols_legacy

    x = np.asarray(sizes, dtype=np.int64)
    x = x[x > 0]
    rec: Dict = {"primary_estimator": "discrete_MLE_KS_xmin", "n_total": int(len(x)),
                 "gamma_OLS_legacy": gamma_ols_legacy(x), "bootstrap_seed": int(seed),
                 "bootstrap_count_ci": int(n_boot_ci), "bootstrap_count_gof": int(n_boot_gof),
                 "min_tail": min_tail, "min_distinct": min_distinct, "S_max": None,
                 "S_max_policy": "not used: pure and truncated models fitted to the whole tail above S_min"}
    scan = scan_xmin(x, min_tail, min_distinct, max_candidates)
    rec["xmin_scan_table"] = scan["table"]
    if scan["best"] is None:
        rec.update(status="insufficient_data", gamma_MLE=None, gamma_ci_low=None, gamma_ci_high=None,
                   S_min=None, S_c=None, ks_statistic=None, gof_p_value=None, n_tail=0, fitted_range=None,
                   fitted_range_decades=None, log_likelihood=None, model_comparison={})
        return rec
    b = scan["best"]
    xmin = b["xmin"]
    tail = _tail(x, xmin)
    ci = bootstrap_gamma_ci(x, xmin, n_boot_ci, seed) if n_boot_ci > 0 else {"ci_low": None, "ci_high": None}
    gof = gof_pvalue(x, n_boot_gof, seed + 1, min_tail, min_distinct, max_candidates) if n_boot_gof > 0 else {"p_value": None}
    cmp_tr = compare_power_law_vs_truncated(x, xmin)
    decades = float(np.log10(tail.max() / xmin)) if xmin > 0 else None
    rec.update(
        status="ok",
        gamma_MLE=b["gamma"], gamma_ci_low=ci["ci_low"], gamma_ci_high=ci["ci_high"], gamma_ci=ci,
        S_min=int(xmin), S_c=cmp_tr["truncated_fit"]["s_c"], truncated_power_law=cmp_tr["truncated_fit"],
        ks_statistic=b["ks"], gof_p_value=gof["p_value"], gof=gof, n_tail=int(len(tail)),
        n_distinct_tail=int(len(np.unique(tail))), fitted_range=[int(xmin), int(tail.max())],
        fitted_range_decades=decades, log_likelihood=b["loglik"],
        alternatives={"exponential": fit_exponential(x, xmin), "lognormal": fit_lognormal(x, xmin)},
        model_comparison={
            "power_law_vs_truncated": {k: v for k, v in cmp_tr.items() if k != "truncated_fit"},
            "power_law_vs_exponential": compare_models(x, xmin, "power_law", "exponential"),
            "power_law_vs_lognormal": compare_models(x, xmin, "power_law", "lognormal"),
            "truncated_vs_lognormal": compare_models(x, xmin, "truncated_power_law", "lognormal"),
        },
        data_requirements={"n_tail_ok": len(tail) >= min_tail,
                           "distinct_ok": len(np.unique(tail)) >= min_distinct,
                           "decades_ok": decades is not None and decades >= min_decades},
    )
    return rec
