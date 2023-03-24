import json
import sys
import math
import numbers
import os
from abc import abstractmethod
from functools import partial
from numbers import Real
from typing import List, Tuple, Union
from numpy.typing import ArrayLike
import numpy as np
import pandas as pd
from survhive._utils_.hyperparams import (
    CVSCORERFACTORY,
    ESTIMATORFACTORY,
    OPTIMISERFACTORY,
)
from numpy import ndarray
from scipy import sparse
from sklearn.linear_model._base import _preprocess_data
from sklearn.linear_model._coordinate_descent import LinearModelCV, _check_sample_weight
from sklearn.model_selection import check_cv
from sklearn.utils.extmath import safe_sparse_dot
from sklearn.utils.parallel import Parallel, delayed
from sklearn.utils.validation import check_array, check_consistent_length, check_scalar
from typeguard import typechecked
from survhive._utils_.scorer import *
from survhive.optimiser import Optimiser


def _alpha_grid(
    X: ArrayLike,
    y: ArrayLike,
    Xy: ArrayLike = None,
    eps: float = 1e-3,
    n_alphas: int = 100,
) -> np.array:
    """Compute the grid of alpha values for model parameter search
    Parameters

    Args:
        X (ArrayLike): Array-like object of training data of shape (n_samples, n_features).
        y (ArrayLike): Array-like object of target values of shape (n_samples,) or (n_samples, n_outputs).
        Xy (ArrayLike, optional): Dot product of X and y arrays having shape (n_features,)
        or (n_features, n_outputs). Defaults to None.
        eps (float, optional): Length of the path. ``eps=1e-3`` means that
        ``alpha_min / alpha_max = 1e-3``. Defaults to 1e-3.
        n_alphas (int, optional): Number of alphas along the regularization path. Defaults to 100.

    Returns:
        np.array: Regularisation parameters to try for the model.
    """

    n_samples = len(y)

    if Xy is None:
        X_sparse = sparse.isspmatrix(X)
        if not X_sparse:
            X, y, _, _, _ = _preprocess_data(X, y, fit_intercept=False, copy=False)
        Xy = safe_sparse_dot(X.T, y, dense_output=True)

    if Xy.ndim == 1:
        Xy = Xy[:, np.newaxis]

    ## Calculate alpha path (first get alpha_max):
    ## sum/max needs an iterable
    alpha_max = max(abs(Xy.sum(axis=-1)) / n_samples)

    if alpha_max <= np.finfo(float).resolution:
        alphas = np.empty(n_alphas)
        alphas.fill(np.finfo(float).resolution)
        return alphas

    alphas = np.round(
        np.logspace(np.log10(alpha_max * eps), np.log10(alpha_max), num=n_alphas)[::-1],
        decimals=10,
    )

    return alphas


def regularisation_path(
    X: ArrayLike,
    y: ArrayLike,
    X_test: ArrayLike,
    model: object,
    *,
    l1_ratio: float = 1.0,
    eps: float = 1e-3,
    n_alphas: int = 100,
    alphas: np.ndarray = None,
    Xy: ArrayLike = None,
    sample_weight=None,
) -> Tuple:
    """Compute estimator path with coordinate descent.

    Args:
        X (ArrayLike): Training data of shape (n_samples, n_features).
        y (ArrayLike): Target values of shape (n_samples,) or (n_samples, n_targets).
        X_test (ArrayLike): Test data of shape (n_samples, n_features).
        model (object): The model object pre-initialised to fit the data for each alpha
            and learn the coefficients.
        l1_ratio (Union[float, ArrayLike], optional): Scaling between l1 and l2 penalties.
            ``l1_ratio=1`` corresponds to the Lasso. Defaults to 0.5.
        eps (float, optional) : Length of the path. Defaults to 1e-3.
        n_alphas (int, optional): Number of alphas along the regularization path.
            Defaults to 100.
        alphas (np.ndarray, optional): List of alphas where to compute the models.
            Defaults to None. If None alphas are set automatically.
        Xy (ArrayLike, optional): Dot product between X and y, of shape (n_features,) or
            (n_features, n_targets). Defaults to None.
        sample_weight (np.array): The weights for samples.


    Returns:
        Tuple: Tuple of the dot products of train and test samples with the coefficients
            learned during training.


    """

    n_samples, n_features = X.shape
    test_samples, _ = X_test.shape

    if alphas is None:
        alphas = _alpha_grid(X, y, Xy=Xy, l1_ratio=l1_ratio, eps=eps, n_alphas=n_alphas)
    elif len(alphas) > 1:
        alphas = np.sort(alphas)[::-1]

    n_alphas = len(alphas)

    coefs = np.empty((n_features, n_alphas), dtype=X.dtype)
    train_eta = np.empty((n_samples, n_alphas), dtype=X.dtype)
    test_eta = np.empty((test_samples, n_alphas), dtype=X.dtype)

    if sample_weight is None:
        sample_weight = np.ones(X.shape[0])

    model.__setattr__("warm_start", True)
    model.__setattr__("l1_ratio", l1_ratio)

    for i, alpha in enumerate(alphas):
        model.__setattr__("alpha", alpha)
        model.fit(X, y, sample_weight=sample_weight)

        coefs[..., i] = model.coef_
        train_eta[..., i] = model.predict(X)
        test_eta[..., i] = model.predict(X_test)

    return train_eta, test_eta


def _path_xcoefs(
    X: ArrayLike,
    y: ArrayLike,
    model: object,
    sample_weight: ArrayLike,
    train: List,
    test: List,
    path: callable,
    path_params: dict,
    alphas: ArrayLike = None,
    l1_ratio: float = 1.0,
) -> Tuple:
    """Returns the dot product of samples and coefs for the models computed by 'path'.

    Args:
        X (ArrayLike): Training data of shape (n_samples, n_features).
        y (ArrayLike): Target values of shape (n_samples,) or (n_samples, n_targets).
        model (object): The model object pre-initialised to fit the data for each alpha
            and learn the coefficients.
        sample_weight (ArrayLike): Sample weights of shape (n_samples,). Pass None if
            there are no weights.
        train (List): The indices of the train set.
        test (List): The indices of the test set.
        path (callable): Function returning a list of models on the path. See
        enet_path for an example of signature.
        path_params (dict): Parameters passed to the path function.
        alphas (ArrayLike, optional): Array of float that is used for cross-validation. If not
        provided, computed using 'path'. Defaults to None.
        l1_ratio (Union[float,List], optional): Scaling between
        l1 and l2 penalties. For ``l1_ratio = 0`` the penalty is an
        L2 penalty. For ``l1_ratio = 1`` it is an L1 penalty. For ``0
        < l1_ratio < 1``, the penalty is a combination of L1 and L2. Defaults to 1.

    Returns:
        Tuple: Tuple of the dot products of train and test samples with the coefficients
            learned during training, and the associated target values for train and test.
    """
    X_train = X[train]
    y_train = y[train]
    X_test = X[test]
    y_test = y[test]
    if sample_weight is None:
        sw_train, sw_test = None, None
    else:
        sw_train = sample_weight[train]
        sw_test = sample_weight[test]
        n_samples = X_train.shape[0]

        sw_train *= n_samples / np.sum(sw_train)

    if not sparse.issparse(X):
        for array, array_input in (
            (X_train, X),
            (y_train, y),
            (X_test, X),
            (y_test, y),
        ):
            if array.base is not array_input and not array.flags["WRITEABLE"]:
                array.setflags(write=True)

    path_params_cp = path_params.copy()
    path_params["copy_X"] = False
    path_params_cp["alphas"] = alphas
    path_params_cp["sample_weight"] = sw_train

    if "l1_ratios" in path_params_cp:
        path_params_cp.pop("l1_ratios")

    path_params_cp["l1_ratio"] = l1_ratio
    path_params_cp["Xy"] = None

    path_params_cp.pop("optimiser")
    path_params_cp.pop("random_state")
    path_params_cp.pop("copy_X")
    path_params_cp.pop("cv_score_method")
    path_params_cp.pop("tol")
    path_params_cp.pop("max_iter")

    train_eta, test_eta = path(X_train, y_train, X_test, model, **path_params_cp)

    return train_eta, test_eta, y_train, y_test


class CrossValidation(LinearModelCV):
    """Cross validation class with custom scoring functions."""

    @abstractmethod
    def __init__(
        self,
        optimiser: str,
        cv_score_method: str = "linear_predictor",
        eps: float = 1e-3,
        n_alphas: int = 100,
        alphas: ArrayLike = None,
        l1_ratios: Union[float, ArrayLike] = None,
        max_iter: int = 1000,
        tol: float = 1e-4,
        copy_X: bool = True,
        cv: Union[
            int, object
        ] = None,  # INFO: if task is classification, then StratifiedKFold is used.
        n_jobs: int = None,
        random_state: int = None,
    ) -> None:
        """Constructor.

        Args:
            optimiser (str): Optimiser to use for model fitting. See OPTIMISERFACTORY for
                options.
            cv_score_method (str): CV scoring method to use for model selection. One of
                ["linear_predictor","regular","vvh"]. Defaults to "linear_predictor".
            eps (float, optional): Length of the path. ``eps=1e-3`` means that
                ``alpha_min / alpha_max = 1e-3``. Defaults to 1e-3.
            n_alphas (int, optional): Number of alphas along the regularization path.
                Defaults to 100.
            alphas (ArrayLike, optional): Array of float that is used for cross-validation. If not
                provided, computed using 'path'. Defaults to None.
            l1_ratios (Union[float,ArrayLike], optional): Scaling between
                l1 and l2 penalties. For ``l1_ratio = 0`` the penalty is an
                L2 penalty. For ``l1_ratio = 1`` it is an L1 penalty. For ``0
                < l1_ratio < 1``, the penalty is a combination of L1 and L2. Defaults to None.
            max_iter (int, optional): The maximum number of iterations of the estimator.
                Defaults to 1000.
            tol (float, optional): The tolerance for the optimization. Defaults to 1e-4.
            copy_X (bool, optional): Creates a copy of X if True. Defaults to True.
            cv (Union[int,object], optional): Cross validation splitting strategy.
                Defaults to None, which uses the default 5-fold cv. Can also pass cv-generator.
            n_jobs (int, optional): Number of CPUs to use during the cross validation. Defaults to None.
            random_state (int, optional): The seed of the pseudo random number generator that selects a random
                feature to update. Defaults to None.
        """

        super().__init__(
            eps=eps,
            n_alphas=n_alphas,
            alphas=alphas,
            fit_intercept=False,
            max_iter=max_iter,
            tol=tol,
            copy_X=copy_X,
            cv=cv,
            n_jobs=n_jobs,
            random_state=random_state,
        )

        self.eps = eps
        self.optimiser = optimiser
        self.n_alphas = n_alphas
        self.alphas = alphas
        self.l1_ratios = l1_ratios
        if isinstance(self.l1_ratios, float):
            self.l1_ratios = list(self.l1_ratios)
        self.fit_intercept = False
        self.cv_score_method = cv_score_method
        self.max_iter = max_iter
        self.tol = tol
        self.copy_X = copy_X
        self.cv = cv

        self.n_jobs = n_jobs

        self.random_state = random_state

    def fit(
        self, X: ArrayLike, y: ArrayLike, sample_weight: Union[float, ArrayLike] = None
    ) -> object:
        """Fit linear model.
        Fit is on grid of alphas and best alpha estimated by cross-validation.

        Args:
            X (ArrayLike): Training data of shape (n_samples, n_features).
            y (ArrayLike): Target values of shape (n_samples,) or (n_samples, n_targets).
            sample_weight (Union[float,ArrayLike]): Sample weights used for fitting and evaluation of the weighted
                mean squared error of each cv-fold. Has shape (n_samples,) and defaults
                to None.

        Returns:
            self(object): Returns an instance of fitted model.
        """

        self._validate_params()

        check_consistent_length(X, y)

        if isinstance(sample_weight, numbers.Number):
            sample_weight = None
        if sample_weight is not None:
            sample_weight = _check_sample_weight(sample_weight, X, dtype=X.dtype)

        model = self._get_estimator()

        path_params = self.get_params()

        path_params.pop("fit_intercept", None)

        if "l1_ratios" in path_params:
            l1_ratios = np.atleast_1d(path_params["l1_ratios"])

            path_params["l1_ratios"] = l1_ratios
        else:
            l1_ratios = [
                1.0,
            ]

        path_params.pop("cv", None)
        path_params.pop("n_jobs", None)

        alphas = self.alphas
        n_l1_ratios = len(l1_ratios)

        check_scalar_alpha = partial(
            check_scalar,
            target_type=Real,
            min_val=0.0,
            include_boundaries="left",
        )

        if alphas is None:
            alphas = [
                _alpha_grid(
                    X,
                    y,
                    Xy=None,
                    eps=self.eps,
                    n_alphas=self.n_alphas,
                )
                for l1_ratio in l1_ratios
            ]
        else:

            for index, alpha in enumerate(alphas):
                check_scalar_alpha(alpha, f"alphas[{index}]")

            alphas = np.tile(np.sort(alphas)[::-1], (n_l1_ratios, 1))

        n_alphas = len(alphas[0])
        path_params.update({"n_alphas": n_alphas})

        cv = check_cv(self.cv)

        folds = list(cv.split(X, y))
        best_pl_score = 0.0

        jobs = (
            delayed(_path_xcoefs)(
                X,
                y,
                model,
                sample_weight,
                train,
                test,
                regularisation_path,
                path_params,
                alphas=this_alphas,
                l1_ratio=this_l1_ratio,
            )
            for this_l1_ratio, this_alphas in zip(l1_ratios, alphas)
            for train, test in folds
        )

        xcoefs_path = Parallel(
            n_jobs=self.n_jobs,
            verbose=self.verbose,
            prefer="threads",
        )(jobs)

        train_xb, test_xb, train_y, test_y = zip(*xcoefs_path)
        n_folds = int(len(train_xb) / len(l1_ratios))

        mean_cv_score_l1 = []
        mean_cv_score = []

        for i in range(len(l1_ratios)):

            train_eta = train_xb[n_folds * i : n_folds * (i + 1)]
            test_eta = test_xb[n_folds * i : n_folds * (i + 1)]
            train_y_l1 = train_y[n_folds * i : n_folds * (i + 1)]
            test_y_l1 = test_y[n_folds * i : n_folds * (i + 1)]

            if self.cv_score_method == "linear_predictor":
                train_eta_lp = np.concatenate(train_eta)
                test_eta_lp = np.concatenate(test_eta)
                train_y_lp = np.concatenate(train_y_l1)
                test_y_lp = np.concatenate(test_y_l1)
                train_time, train_event = inverse_transform_survival(train_y_lp)
                test_time, test_event = inverse_transform_survival(test_y_lp)

                for j in range(len(alphas[i])):

                    # pass model.loss to do model.loss
                    likelihood = CVSCORERFACTORY[self.cv_score_method](
                        test_eta_lp[:, j], test_time, test_event, model.loss
                    )
                    mean_cv_score_l1.append(likelihood)

                mean_cv_score.append(mean_cv_score_l1)

            else:
                test_fold_likelihoods = []
                for k in range(n_folds):
                    train_eta_nlp = train_eta[k]
                    test_eta_nlp = test_eta[k]
                    train_y_nlp = train_y_l1[k]
                    test_y_nlp = test_y_l1[k]

                    train_time, train_event = inverse_transform_survival(train_y_nlp)
                    test_time, test_event = inverse_transform_survival(test_y_nlp)
                    for j in range(len(alphas[i])):
                        fold_likelihood = CVSCORERFACTORY[self.cv_score_method](
                            test_eta_nlp[:, j],
                            test_time,
                            test_event,
                            train_eta_nlp[:, j],
                            train_time,
                            train_event,
                            model.loss,
                        )
                        test_fold_likelihoods.append(fold_likelihood)
                    mean_cv_score_l1.append(np.mean(test_fold_likelihoods))
                mean_cv_score.append(mean_cv_score_l1)

        self.pl_path_ = mean_cv_score
        for l1_ratio, l1_alphas, pl_alphas in zip(l1_ratios, alphas, mean_cv_score):
            i_best_alpha = np.argmax(mean_cv_score)
            this_best_pl = pl_alphas[i_best_alpha]
            if this_best_pl < best_pl_score:
                best_alpha = l1_alphas[i_best_alpha]
                best_l1_ratio = l1_ratio
                best_pl_score = this_best_pl

        self.l1_ratio_ = best_l1_ratio
        self.alpha_ = best_alpha
        if self.alphas is None:
            self.alphas_ = np.asarray(alphas)
            if n_l1_ratios == 1:
                self.alphas_ = self.alphas_[0]

        else:
            self.alphas_ = np.asarray(alphas[0])

        # Refit the model with the parameters selected
        common_params = {
            name: value
            for name, value in self.get_params().items()
            if name in model.get_params()
        }
        model.set_params(**common_params)
        model.alpha = best_alpha
        model.l1_ratio = best_l1_ratio

        if sample_weight is None:
            model.fit(X, y)
        else:
            model.fit(X, y, sample_weight=sample_weight)

        if not hasattr(self, "l1_ratio"):
            del self.l1_ratio_

        self.coef_ = model.coef_
        self.intercept_ = model.intercept_

        return self
