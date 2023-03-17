from typing import List, Optional, Union

import numpy as np
import pandas as pd
from sklearn.linear_model._base import LinearModel
from typeguard import typechecked

from .optimiser import Optimiser
from .utils import inverse_transform_survival


@typechecked
class RegularizedLinearSurvivalModel(LinearModel):
    def __init__(
        self,
        alpha: float,
        optimiser: str,
        l1_ratio: float = 1.0,
        groups: Optional[List[List[int]]] = None,
        line_search: bool = True,
        line_search_reduction_factor: float = 0.5,
        warm_start: bool = True,
        max_iter: int = 1000,
        tol: float = 1e-7,
        verbose: int = 0,
        random_state: Optional[Union[int, np.random.RandomState]] = None,
    ):
        self.alpha: float = alpha
        self.optimiser: str = optimiser
        self.l1_ratio: float = l1_ratio
        self.groups: Optional[List[List[int]]] = groups
        self.line_search: bool = line_search
        self.line_search_reduction_factor: float = line_search_reduction_factor
        self.warm_start: bool = warm_start
        self.max_iter: int = max_iter
        self.tol: float = tol
        self.verbose: int = verbose
        self.random_state: Optional[Union[int, np.random.RandomState]] = random_state

    def fit(self, X: pd.DataFrame, y: np.array, sample_weight: np.array = None) -> None:
        """Fit model with proximal gradient descent.

        Parameters
        ----------
        X : pd.DataFrame of (n_samples, n_features)
            Data.
        y : np.array of shape (n_samples,)
            Target. Will be cast to X's dtype if necessary.
        sample_weight : np.array of shape (n_samples,), default=None
            Sample weights. Internally, the `sample_weight` vector will be
            rescaled to sum to `n_samples`.
        check_input : bool, default=True
            Allow to bypass several input checking.
            Don't use this parameter unless you know what you do.

        Notes
        ---
        To be implemented in each child class.
        """
        optimiser: Optimiser = Optimiser(
            grad=self.gradient,
            optimiser=self.optimiser,
            line_search=self.line_search,
            max_iter=self.max_iter,
            tol=self.tol,
            random_state=self.random_state,
            alpha=self.alpha,
            groups=self.groups,
        )
        self.coef_: np.array = optimiser.optimise(X=X, y=y, sample_weight=sample_weight)
        self.train_event = (np.abs(y) == y).astype(int)
        self.train_eta = self.predict(X)

    def predict_cumulative_hazard_function(
        self, X: np.array, time: np.array
    ) -> pd.DataFrame:
        """Predict cumulative hazard function for each sample and each requested time.

        Parameters
        ----------
        X : pd.DataFrame of (n_samples, n_features)
            Data.
        time : np.array of (n_times)
            Times at which hazard function predictions are desired.

        Returns
        ---
        cumulative_hazard_function : NDArray[Shape["*", "*"], Float32] of (n_samples, n_times)
            Cumulative hazard function for each sample and each requested time.

        Notes
        ---
        To be implemented in each child class.
        """
        raise NotImplementedError

    def predict_survival_function(self, X: np.array, time: np.array) -> pd.DataFrame:
        """Predict survival function for each sample and each requested time.

        Parameters
        ----------
        X : pd.DataFrame of (n_samples, n_features)
            Data.
        time : np.array of (n_times)
            Times at which hazard function predictions are desired.

        Returns
        ---
        survival_function : pd.DataFrame of (n_samples, n_times)
            Survival function for each sample and each requested time.

        Notes
        ---
        We exclusively rely on `predict_cumulative_hazard_function`
        and simply transform this to the survival function.
        """
        time_sorted: np.array = np.sort(a=time, kind="stable")
        return np.exp(
            np.negative(self.predict_cumulative_hazard_function(X=X, time=time_sorted))
        )

    def score(self, X, y, sample_weight=None):
        time: np.array
        event: np.array
        time, event = inverse_transform_survival(y=y)
        sorted_indices: np.array = np.argsort(a=time, kind="stable")
        time_sorted: np.array = time[sorted_indices]
        event_sorted: np.array = event[sorted_indices]
        X_sorted: np.array = X[
            sorted_indices,
        ]
        if sample_weight is None:
            sample_weight: np.array = np.ones(time.shape[0]) / time.shape[0]
        # Flip the loss by turning it into the likelihood
        # again since score implies higher values are better.
        return np.negative(
            self.loss(
                linear_predictor=self.predict(X_sorted),
                time=time_sorted,
                event=event_sorted,
                sample_weight=sample_weight,
            )
        )
