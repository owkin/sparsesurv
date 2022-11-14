from typing import List

import numpy as np
from numba import jit

# TODO:
# - Figure out whether the latent group lasso stuff will work
# for everything?
# - Other biselection regularizers


@jit(nopython=True, cache=True)
def _soft_threshold(x: np.array, threshold: float) -> np.array:
    return np.sign(x) * np.maximum(np.abs(x) - threshold, 0.0)


@jit(nopython=True, cache=True)
def _soft_threshold_group(x: np.array, threshold: float) -> np.array:
    return np.sign(x) * np.maximum(
        np.abs(x) - threshold * (x / np.linalg.norm(x, ord=2)), 0.0
    )


@jit(nopython=True, cache=True)
def _scad_thresh(x: np.array, threshold: float, a: float) -> np.array:
    lower_mask: np.array = np.abs(x) <= (2.0 * threshold)
    upper_mask: np.array = np.abs(x) > (threshold * a)
    middle_mask: np.array = (
        np.ones(lower_mask.shape) - upper_mask - middle_mask
    )
    return (
        _soft_threshold(x=x, threshold=threshold) * lower_mask
        + ((a - 1) / (a - 2))
        * _soft_threshold(x=x, threshold=(a * threshold) / (a - 1))
        * middle_mask
        + x * upper_mask
    )


@jit(nopython=True, cache=True)
def _scad_thresh_group(x: np.array, threshold: float, a: float) -> np.array:
    lower_mask: np.array = np.abs(x) <= (2.0 * threshold)
    upper_mask: np.array = np.abs(x) > (threshold * a)
    middle_mask: np.array = (
        np.ones(lower_mask.shape) - upper_mask - middle_mask
    )
    return (
        _soft_threshold_group(x=x, threshold=threshold) * lower_mask
        + ((a - 1) / (a - 2))
        * _soft_threshold_group(x=x, threshold=(a * threshold) / (a - 1))
        * middle_mask
        + x * upper_mask
    )


@jit(nopython=True, cache=True)
def _mcp_thresh(x: np.array, threshold: float, gamma: float):
    mask: np.array = np.sign(x) > (threshold * gamma)
    return (gamma / (gamma - 1)) * _soft_threshold(
        x=x, threshold=threshold
    ) * (1 - mask) + x * mask


@jit(nopython=True, cache=True)
def _mcp_thresh_group(x: np.array, threshold: float, gamma: float) -> np.array:
    mask: np.array = np.sign(x) > (threshold * gamma)
    return (gamma / (gamma - 1)) * _soft_threshold_group(
        x=x, threshold=threshold
    ) * (1 - mask) + x * mask


@jit(nopython=True, cache=True)
def _gel_derivative(coef: np.array, threshold: float, tau: float) -> np.array:
    return threshold * np.exp(
        np.negative(tau / threshold) * np.sum(np.abs(coef))
    )


@jit(nopython=True, cache=True)
def _mcp(coef: np.array, threshold: float, gamma: float) -> np.array:
    mask = coef <= gamma * threshold
    return mask * (threshold * gamma - (coef**2 / (2 * gamma))) + (
        1 - mask
    ) * (gamma * (threshold**2) / 2)


@jit(nopython=True, cache=True)
def _mcp_derivative(
    coef: np.array, threshold: float, gamma: float
) -> np.array:
    return np.max(threshold - (coef / gamma), 0)


@jit(nopython=True, cache=True)
def _cmcp_derivative(
    coef: np.array, threshold: float, gamma: float
) -> np.array:
    _mcp_derivative(
        coef=np.sum(_mcp(coef=np.abs(coef), threshold=threshold, gamma=gamma)),
        threshold=threshold,
        gamma=gamma,
    ) * _mcp_derivative(coef=np.abs(coef), threshold=threshold, gamma=gamma)


class ProximalOperator:
    def __init__(self, threshold) -> None:
        self.threshold: float = threshold

    def __call__(self, coef: np.array) -> np.array:
        return NotImplementedError


class LassoProximal(ProximalOperator):
    def __init__(self, threshold: float) -> None:
        super().__init__(threshold)

    def __call__(self, coef: np.array) -> np.array:
        return _soft_threshold(coef, self.threshold)


class GroupLassoProximal(ProximalOperator):
    def __init__(self, threshold: float, groups: List[np.array]) -> None:
        super().__init__(threshold)
        self.groups: List[np.array] = groups

    def __call__(self, coef: np.array) -> np.array:
        for group in self.groups:
            coef[group] = _soft_threshold_group(coef[group], self.threshold)
        return coef


class SGLProximal(ProximalOperator):
    def __init__(
        self, threshold: float, alpha: float, groups: List[np.array]
    ) -> None:
        super().__init__(threshold)
        self.alpha: float = alpha
        self.groups: List[np.array] = groups

    def __call__(self, coef: np.array) -> np.array:
        return GLProximal(
            threshold=self.threshold * (1 - self.alpha), groups=self.groups
        )(coef) * LassoProximal(threshold=self.threshold * self.alpha)(coef)


class SCADProximal(ProximalOperator):
    def __init__(self, threshold: float, a: float = 3.7) -> None:
        super().__init__(threshold)
        self.a: float = a

    def __call__(self, coef: np.array) -> np.array:
        _scad_thresh(coef, threshold=self.threshold, a=self.a)


class GSCADProximal(ProximalOperator):
    def __init__(
        self, threshold: float, groups: List[np.array], a: float = 3.7
    ) -> None:
        super().__init__(threshold)
        self.a: float = a
        self.groups: List[np.array] = groups

    def __call__(self, coef: np.array) -> np.array:
        for group in self.groups:
            coef[group] = _scad_thresh_group(
                coef[group], threshold=self.threshold, gamma=self.gamma
            )
        return coef


class MCPProximal(ProximalOperator):
    def __init__(self, threshold: float, gamma=4.0) -> None:
        super().__init__(threshold)
        self.gamma: float = gamma

    def __call__(self, coef: np.array) -> np.array:
        _mcp_thresh(coef, threshold=self.threshold, gamma=self.gamma)


class GMCPProximal(ProximalOperator):
    def __init__(self, threshold: float, gamma=4.0) -> None:
        super().__init__(threshold)
        self.gamma: float = gamma

    def __call__(self, coef: np.array) -> np.array:
        for group in self.groups:
            coef[group] = _mcp_thresh_group(
                coef[group], threshold=self.threshold, gamma=self.gamma
            )
        return coef


class CMCPProximal(ProximalOperator):
    def __init__(self, threshold: float, gamma=4.0) -> None:
        super().__init__(threshold)
        self.gamma: float = gamma

    def __call__(self, coef: np.array) -> np.array:
        for group in self.groups:
            coef[group] = _soft_threshold(
                coef[group],
                threshold=_cmcp_derivative(
                    coef=coef[group],
                    threshold=self.threshold,
                    gamma=self.gamma,
                ),
            )
        return coef


class GELProximal(ProximalOperator):
    # TODO: Double check whether this actually results in sparse
    # groups.
    def __init__(
        self, threshold: float, groups: List[np.array], tau: float = 1 / 3
    ) -> None:
        super().__init__(threshold)
        self.tau: float = tau
        self.groups = groups

    def __call__(self, coef: np.array) -> np.array:
        for ix, group in self.groups:
            coef[group] = _soft_threshold(
                x=coef[group],
                threshold=_gel_derivative(
                    coef=coef[group], threshold=self.threshold, tau=self.tau
                ),
            )
        return coef
