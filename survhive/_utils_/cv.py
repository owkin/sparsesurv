import numbers
from abc import abstractmethod
from functools import partial
from numbers import Real
from typing import List, Tuple, Union
from collections.abc import Iterable

import numpy as np
import pandas as pd
from survhive._utils_.hyperparams import (
    CVSCORERFACTORY,
    # ESTIMATORFACTORY,
    # OPTIMISERFACTORY,
)
from numpy.typing import ArrayLike
from numpy import ndarray
from scipy import sparse
from sklearn.linear_model._base import _preprocess_data
from sklearn.linear_model._coordinate_descent import LinearModelCV, _check_sample_weight
from sklearn.model_selection import StratifiedKFold
from sklearn.model_selection._split import _CVIterableWrapper
from sklearn.utils.extmath import safe_sparse_dot
from sklearn.utils.parallel import Parallel, delayed
from sklearn.utils.validation import check_consistent_length, check_scalar
from typeguard import typechecked

from survhive._utils_.hyperparams import CVSCORERFACTORY
from survhive._utils_.scorer import *
from survhive.optimiser import Optimiser
from ..constants import EPS
from sklearn.linear_model._coordinate_descent import _set_order
from ..screening import StrongScreener
from sklearn.linear_model import ElasticNet as ScikitElasticNet
from celer import ElasticNet
from glum import GeneralizedLinearRegressor


def _alpha_grid(
    X: ArrayLike,
    y: ArrayLike,
    gradient,
    hessian,
    l1_ratio,
    Xy: ArrayLike = None,
    eps: float = 0.05,
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
    hessian_mask: np.array = (hessian > 0).astype(bool)
    alpha_max = np.max(
        np.abs(np.matmul(gradient.T[hessian_mask], X[hessian_mask, :]))
    ) / (np.sum(hessian[hessian_mask]))
    if alpha_max <= np.finfo(float).resolution:
        alphas = np.empty(n_alphas)
        alphas.fill(np.finfo(float).resolution)
        return alphas

    eps = 0.1
    n_alphas = 100
    alphas = np.round(
        np.logspace(
            np.log10((alpha_max + 1e-9) * eps), np.log10(alpha_max + 1e-9), num=n_alphas
        )[::-1],
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
    eps: float = 0.05,
    n_alphas: int = 100,
    alphas: np.ndarray = None,
    Xy: ArrayLike = None,
    sample_weight=None,
    n_irls_iter=10,
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
    time, event = inverse_transform_survival(y)
    eta_previous = np.zeros(X.shape[0])
    gradient, hessian = model.gradient(
        linear_predictor=eta_previous,
        time=time,
        event=event,
    )
    if alphas is None:
        alphas = _alpha_grid(
            X,
            y,
            Xy=Xy,
            l1_ratio=l1_ratio,
            eps=0.05,
            n_alphas=n_alphas,
            gradient=gradient,
            hessian=hessian,
        )
    elif len(alphas) > 1:
        alphas = np.sort(alphas)[::-1]
    n_alphas = len(alphas)

    coefs = np.zeros((n_features, n_alphas), dtype=X.dtype)
    train_eta = np.empty((n_samples, n_alphas), dtype=X.dtype)
    test_eta = np.empty((test_samples, n_alphas), dtype=X.dtype)
    beta_previous = np.zeros(X.shape[1])
    strong_screener = StrongScreener(p=X.shape[1])
    optimiser = ScikitElasticNet(
        l1_ratio=l1_ratio, fit_intercept=False, warm_start=True
    )
    for i, alpha in enumerate(alphas):
        # print(alpha)
        # print(eta_previous)
        optimiser.__setattr__("alpha", alpha)
        print(f"Starting alpha: {i}")
        for q in range(n_irls_iter):
            print(f"Starting IRLS: {q}")
            gradient: np.array
            hessian: np.array
            gradient, hessian = model.gradient(
                linear_predictor=eta_previous,
                time=time,
                event=event,
            )
            inverse_hessian = hessian.copy()
            hessian_mask = (hessian > 0).astype(bool)
            inverse_hessian[np.logical_not(hessian_mask)] = np.inf
            inverse_hessian = 1 / inverse_hessian
            inverse_hessian[inverse_hessian == 1 / np.inf] = 0
            weights = hessian[hessian_mask]
            correction_factor = np.sum(weights)
            weights = weights * (np.sum(hessian_mask) / np.sum(weights))
            weights_sqrt = np.sqrt(weights)

            # weights_sqrt_matrix = weights_sqrt.repeat(X.shape[1]).reshape((np.sum(hessian_mask), X.shape[1]))
            weights_sqrt_matrix = np.expand_dims(weights_sqrt, 1).repeat(X.shape[1], 1)
            # print(weights_sqrt_matrix.shape)
            if i + q < 1:
                X_irls = X[hessian_mask, :] * weights_sqrt_matrix
                y_irls = (
                    weights_sqrt_matrix[:, 0]
                    * (eta_previous - inverse_hessian * gradient)[hessian_mask]
                )
            # Update them somehow
            elif (hessian_mask == hessian_mask_old).all():
                X_irls = X_irls * weights_sqrt_matrix / weights_sqrt_matrix_old
                y_irls = (
                    y_irls * weights_sqrt_matrix[:, 0] / weights_sqrt_matrix_old[:, 0]
                )
            else:
                X_irls = X[hessian_mask, :] * weights_sqrt.repeat(X.shape[1]).reshape(
                    (np.sum(hessian_mask), X.shape[1])
                )
                y_irls = (
                    weights_sqrt
                    * (eta_previous - inverse_hessian * gradient)[hessian_mask]
                )
            eta_previous = eta_previous[hessian_mask]
            if i + q > 0 and i + q < 2:
                strong_screener.compute_strong_set(
                    X=X_irls,
                    y=y_irls,
                    eta_previous=eta_previous,
                    alpha=alpha,
                    alpha_previous=alpha_previous,
                )
                # warm_start_coef = beta_previous[strong_screener.working_set]
                optimiser.coef_ = np.zeros(strong_screener.strong_set.shape[0])

                optimiser.fit(X=X_irls[:, strong_screener.strong_set], y=y_irls)
                # Compute strong set only using the previously active set.
                # strong_screener.compute_strong_set(X=X_irls, y=y_irls, eta_previous=eta_previous, alpha=alpha, alpha_previous=alpha_previous)
                # eta_new: np.array = np.matmul(X_irls[:, strong_screener.working_set], optimiser.coef_)
                strong_screener.expand_working_set(strong_screener.strong_set)
                eta_new: np.array = np.matmul(
                    X_irls[:, strong_screener.strong_set], optimiser.coef_
                )
                while True:
                    strong_screener.check_kkt_all(
                        X=X_irls, y=y_irls, eta=eta_new, alpha=alpha
                    )
                    # If there are violations, add the violators to the
                    # working set and recompute the strong set based on them
                    # and go back to the start.
                    if strong_screener.any_kkt_violated.shape[0] > 0:
                        # print("HEY")
                        # warm_start_coef_ = np.zeros(strong_screener.working_set.shape[0] + strong_screener.any_kkt_violated.shape[0])
                        warm_start_coef = np.zeros(X_irls.shape[1])
                        warm_start_coef[strong_screener.working_set] = optimiser.coef_
                        strong_screener.expand_working_set_with_overall_violations()
                        warm_start_coef = warm_start_coef[strong_screener.working_set]
                        optimiser.coef_ = warm_start_coef
                        optimiser.fit(
                            X=X_irls[:, strong_screener.working_set], y=y_irls
                        )
                        continue
                    break

            elif i + q > 1:
                if not optimiser.warm_start:
                    optimiser.__setattr__("warm_start", True)
                warm_start_coef = beta_previous[strong_screener.working_set]
                optimiser.coef_ = warm_start_coef

                optimiser.fit(X=X_irls[:, strong_screener.working_set], y=y_irls)
                # Compute strong set only using the previously active set.
                strong_screener.compute_strong_set(
                    X=X_irls,
                    y=y_irls,
                    eta_previous=eta_previous,
                    alpha=alpha,
                    alpha_previous=alpha_previous,
                )
                eta_new: np.array = np.matmul(
                    X_irls[:, strong_screener.working_set], optimiser.coef_
                )
                while True:
                    # Check KKT conditions for strong set computed
                    # using only the working set.
                    strong_screener.check_kkt_strong(
                        X=X_irls, y=y_irls, eta=eta_new, alpha=alpha
                    )
                    # If there are violations, add violators to the working
                    # set and refit.
                    if strong_screener.strong_kkt_violated.shape[0] > 0:
                        # warm_start_coef_ = np.zeros(strong_screener.working_set.shape[0] + strong_screener.strong_kkt_violated.shape[0])
                        warm_start_coef = np.zeros(X_irls.shape[1])
                        # warm_start_coef =
                        warm_start_coef[strong_screener.working_set] = optimiser.coef_
                        strong_screener.expand_working_set_with_kkt_violations()
                        warm_start_coef = warm_start_coef[strong_screener.working_set]
                        optimiser.coef_ = warm_start_coef
                        optimiser.fit(
                            X=X_irls[:, strong_screener.working_set],
                            y=y_irls,
                        )
                        continue
                    # Finally, check KKT conditions for all variables.
                    strong_screener.check_kkt_all(
                        X=X_irls, y=y_irls, eta=eta_new, alpha=alpha
                    )
                    # If there are violations, add the violators to the
                    # working set and recompute the strong set based on them
                    # and go back to the start.
                    if strong_screener.any_kkt_violated.shape[0] > 0:
                        # print("HEY")
                        # warm_start_coef_ = np.zeros(strong_screener.working_set.shape[0] + strong_screener.any_kkt_violated.shape[0])
                        warm_start_coef = np.zeros(X_irls.shape[1])
                        warm_start_coef[strong_screener.working_set] = optimiser.coef_
                        strong_screener.expand_working_set_with_overall_violations()
                        warm_start_coef = warm_start_coef[strong_screener.working_set]
                        optimiser.coef_ = warm_start_coef
                        optimiser.fit(
                            X=X_irls[:, strong_screener.working_set], y=y_irls
                        )
                        continue
                    break
            else:
                optimiser.fit(X=X_irls, y=y_irls)
                beta_new = optimiser.coef_
                active_variables = np.where(beta_new != 0)[0]
                eta_final = np.matmul(
                    X[:, active_variables], beta_new[active_variables]
                )
                strong_screener.expand_working_set(active_variables)
                # strong_screener.expand_ever_active_set(active_variables)

            print(np.sum(beta_new))
            learning_rate = 1.0
            # learning_rate = backtracking_line_search(
            #         loss=self.loss,
            #         time=time,
            #         event=event,
            #         current_prediction=eta_final,
            #         previous_prediction=eta,
            #         previous_loss=self.history[-1]["loss"],
            #         reduction_factor=self.line_search_reduction_factor,
            #         max_learning_rate=1.0,
            #         gradient_direction=np.matmul(X.T, gradient),
            #         search_direction=(model.optimiser.coef_ - beta),
            # )

            beta_updated: np.array = (1 - learning_rate) * beta_previous + (
                learning_rate
            ) * beta_new

            # TODO: Adjust this convergence criterion
            if np.max(np.abs(beta_previous - beta_updated)) < 0.001:
                eta_previous = eta_final
                beta_previous = beta_updated
                active_variables = np.where(beta_updated != 0)[0]
                alpha_previous = alpha
                weights_sqrt_matrix_old = weights_sqrt_matrix
                hessian_mask_old = hessian_mask
                break
            else:
                eta_previous = eta_final
                beta_previous = beta_updated
                active_variables = np.where(beta_updated != 0)[0]
                # strong_screener.expand_ever_active_set(active_variables)
                alpha_previous = alpha
                weights_sqrt_matrix_old = weights_sqrt_matrix
                hessian_mask_old = hessian_mask

        coefs[..., i] = beta_previous
        train_eta[..., i] = eta_previous
        test_eta[..., i] = np.matmul(
            X_test[:, active_variables], beta_previous[active_variables]
        )

    return train_eta, test_eta


# def regularisation_path_optimised(
#     X: ArrayLike,
#     y: ArrayLike,
#     X_test: ArrayLike,
#     model: object,
#     *,
#     l1_ratio: float = 1.0,
#     eps: float = 0.05,
#     n_alphas: int = 100,
#     alphas: np.ndarray = None,
#     Xy: ArrayLike = None,
#     sample_weight=None,
#     n_irls_iter=10
# ) -> Tuple:
#     """Compute estimator path with coordinate descent.

#     Args:
#         X (ArrayLike): Training data of shape (n_samples, n_features).
#         y (ArrayLike): Target values of shape (n_samples,) or (n_samples, n_targets).
#         X_test (ArrayLike): Test data of shape (n_samples, n_features).
#         model (object): The model object pre-initialised to fit the data for each alpha
#             and learn the coefficients.
#         l1_ratio (Union[float, ArrayLike], optional): Scaling between l1 and l2 penalties.
#             ``l1_ratio=1`` corresponds to the Lasso. Defaults to 0.5.
#         eps (float, optional) : Length of the path. Defaults to 1e-3.
#         n_alphas (int, optional): Number of alphas along the regularization path.
#             Defaults to 100.
#         alphas (np.ndarray, optional): List of alphas where to compute the models.
#             Defaults to None. If None alphas are set automatically.
#         Xy (ArrayLike, optional): Dot product between X and y, of shape (n_features,) or
#             (n_features, n_targets). Defaults to None.
#         sample_weight (np.array): The weights for samples.


#     Returns:
#         Tuple: Tuple of the dot products of train and test samples with the coefficients
#             learned during training.


#     """

#     n_samples, n_features = X.shape
#     test_samples, _ = X_test.shape
#     time, event = inverse_transform_survival(y)
#     eta_previous = np.zeros(X.shape[0])
#     gradient, hessian = model.gradient(
#                 linear_predictor=eta_previous,
#                 time=time,
#                 event=event,
#             )
#     if alphas is None:
#         alphas = _alpha_grid(X, y, Xy=Xy, l1_ratio=l1_ratio, eps=0.05, n_alphas=n_alphas, gradient=gradient, hessian=hessian)
#     elif len(alphas) > 1:
#         alphas = np.sort(alphas)[::-1]
#     n_alphas = len(alphas)

#     coefs = np.zeros((n_features, n_alphas), dtype=X.dtype)
#     train_eta = np.empty((n_samples, n_alphas), dtype=X.dtype)
#     test_eta = np.empty((test_samples, n_alphas), dtype=X.dtype)
#     beta_previous = np.zeros(X.shape[1])
#     #strong_screener = StrongScreener(p=X.shape[1])
#     eps = np.array([]).astype(int)
#     working_set = np.array([]).astype(int)
#     complete_set = 2
#     optimiser = ScikitElasticNet(l1_ratio=l1_ratio, fit_intercept=False, warm_start=True)
#     for i, alpha in enumerate(alphas):
#         #print(alpha)
#         #print(eta_previous)
#         optimiser.__setattr__("alpha", alpha)
#         print(f"Starting alpha: {i}")
#         for q in range(n_irls_iter):
#             print(f"Starting IRLS: {q}")
#             gradient: np.array
#             hessian: np.array
#             gradient, hessian = model.gradient(
#                 linear_predictor=eta_previous,
#                 time=time,
#                 event=event,
#             )
#             inverse_hessian = hessian.copy()
#             hessian_mask = (hessian > 0).astype(bool)
#             inverse_hessian[np.logical_not(hessian_mask)] = np.inf
#             inverse_hessian = 1 / inverse_hessian
#             inverse_hessian[inverse_hessian == 1 / np.inf] = 0
#             weights = (hessian[hessian_mask])
#             correction_factor = np.sum(weights)
#             weights = weights * (np.sum(hessian_mask) / np.sum(weights))
#             weights_sqrt = np.sqrt(weights)
#             weights_sqrt_matrix = weights_sqrt.repeat(X.shape[1]).reshape((np.sum(hessian_mask), X.shape[1]))
#             if i + q < 1:
#                 X_irls = X[hessian_mask, :] * weights_sqrt_matrix
#                 y_irls = weights_sqrt_matrix[0, :] * (eta_previous - inverse_hessian * gradient)[hessian_mask]
#             # Update them somehow
#             elif hessian_mask == hessian_mask_old:
#                 X_irls = X_irls * weights_sqrt_matrix / weights_sqrt_matrix_old
#                 y_irls = y_irls * weights_sqrt_matrix[0, :] / weights_sqrt_matrix_old[0, :]
#             else:
#                 X_irls = X[hessian_mask, :] * weights_sqrt.repeat(X.shape[1]).reshape((np.sum(hessian_mask), X.shape[1]))
#                 y_irls = weights_sqrt * (eta_previous - inverse_hessian * gradient)[hessian_mask]
#             eta_previous = eta_previous[hessian_mask]
#             if i + q > 1:
#                 if not optimiser.warm_start:
#                     optimiser.__setattr__("warm_start", True)
#                 warm_start_coef = beta_previous[strong_screener.working_set]
#                 optimiser.coef_ = warm_start_coef

#                 optimiser.fit(
#                         X=X_irls[:, strong_screener.working_set],
#                         y=y_irls
#                     )
#                 # Compute strong set only using the previously active set.
#                 strong_screener.compute_strong_set(X=X_irls, y=y_irls, eta_previous=eta_previous, alpha=alpha, alpha_previous=alpha_previous)
#                 eta_new: np.array = np.matmul(X_irls[:, strong_screener.working_set], optimiser.coef_)
#                 while True:
#                     # Check KKT conditions for strong set computed
#                     # using only the working set.
#                     strong_screener.check_kkt_strong(
#                         X=X_irls, y=y_irls, eta=eta_new, alpha=alpha
#                     )
#                     # If there are violations, add violators to the working
#                     # set and refit.
#                     if strong_screener.strong_kkt_violated.shape[0] > 0:
#                         #warm_start_coef_ = np.zeros(strong_screener.working_set.shape[0] + strong_screener.strong_kkt_violated.shape[0])
#                         warm_start_coef = np.zeros(X_irls.shape[1])
#                         #warm_start_coef =
#                         warm_start_coef[strong_screener.working_set] = optimiser.coef_
#                         strong_screener.expand_working_set_with_kkt_violations()
#                         warm_start_coef = warm_start_coef[strong_screener.working_set]
#                         optimiser.coef_ = warm_start_coef
#                         optimiser.fit(
#                                 X=X_irls[:, strong_screener.working_set],
#                                 y=y_irls,
#                             )
#                         continue
#                     # Finally, check KKT conditions for all variables.
#                     strong_screener.check_kkt_all(X=X_irls, y=y_irls, eta=eta_new, alpha=alpha)
#                     # If there are violations, add the violators to the
#                     # working set and recompute the strong set based on them
#                     # and go back to the start.
#                     if strong_screener.any_kkt_violated.shape[0] > 0:
#                         print("HEY")
#                         #warm_start_coef_ = np.zeros(strong_screener.working_set.shape[0] + strong_screener.any_kkt_violated.shape[0])
#                         warm_start_coef = np.zeros(X_irls.shape[1])
#                         warm_start_coef[strong_screener.working_set] = optimiser.coef_
#                         strong_screener.expand_working_set_with_overall_violations()
#                         warm_start_coef = warm_start_coef[strong_screener.working_set]
#                         optimiser.coef_ = warm_start_coef
#                         optimiser.fit(
#                                 X=X_irls[:, strong_screener.working_set],
#                                 y=y_irls
#                             )
#                         continue
#                     break
#             else:
#                 optimiser.fit(
#                         X=X_irls,
#                         y=y_irls
#                     )
#                 beta_new = optimiser.coef_
#                 active_variables = np.where(beta_new != 0)[0]
#                 eta_final = np.matmul(X[:, active_variables], beta_new[active_variables])
#                 #strong_screener.expand_ever_active_set(active_variables)


#             learning_rate = 1.0
#             # learning_rate = backtracking_line_search(
#             #         loss=self.loss,
#             #         time=time,
#             #         event=event,
#             #         current_prediction=eta_final,
#             #         previous_prediction=eta,
#             #         previous_loss=self.history[-1]["loss"],
#             #         reduction_factor=self.line_search_reduction_factor,
#             #         max_learning_rate=1.0,
#             #         gradient_direction=np.matmul(X.T, gradient),
#             #         search_direction=(model.optimiser.coef_ - beta),
#             # )

#             beta_updated: np.array = (1 - learning_rate) * beta_previous + (
#                     learning_rate
#                 ) * beta_new

#             # TODO: Adjust this convergence criterion
#             if np.max(np.abs(beta_previous - beta_updated)) < 0.001:
#                 eta_previous = eta_final
#                 beta_previous = beta_updated
#                 active_variables = np.where(beta_updated != 0)[0]
#                 alpha_previous = alpha
#                 weights_sqrt_matrix_old = weights_sqrt_matrix
#                 break
#             else:
#                 eta_previous = eta_final
#                 beta_previous = beta_updated
#                 active_variables = np.where(beta_updated != 0)[0]
#                 #strong_screener.expand_ever_active_set(active_variables)
#                 alpha_previous = alpha
#                 weights_sqrt_matrix_old = weights_sqrt_matrix


#         coefs[..., i] = beta_previous
#         train_eta[..., i] = eta_previous
#         test_eta[..., i] = np.matmul(X_test[:, active_variables], beta_previous[active_variables])

#     return train_eta, test_eta


def alpha_path_eta(
    X: ArrayLike,
    y: ArrayLike,
    Xy: ArrayLike,
    model: object,
    sample_weight: ArrayLike,
    train: List,
    test: List,
    alphas: ArrayLike = None,
    l1_ratio: float = 1.0,
    eps: float = 1e-3,
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

    n_samples_train, n_features_train = X_train.shape
    n_samples_test, n_features_test = X_test.shape

    if sample_weight is None:
        sw_train, sw_test = None, None
    else:
        sw_train = sample_weight[train]
        sw_test = sample_weight[test]

        sw_train *= n_samples_train / np.sum(sw_train)

    if not sparse.issparse(X):
        for array, array_input in (
            (X_train, X),
            (y_train, y),
            (X_test, X),
            (y_test, y),
        ):
            if array.base is not array_input and not array.flags["WRITEABLE"]:
                array.setflags(write=True)

    if alphas is None:
        alphas = _alpha_grid(X, y, Xy=Xy, l1_ratio=l1_ratio, eps=eps, n_alphas=n_alphas)
    elif len(alphas) > 1:
        alphas = np.sort(alphas)[::-1]

    n_alphas = len(alphas)

    coefs = np.empty((n_features_train, n_alphas), dtype=X.dtype)
    train_eta = np.empty((n_samples_train, n_alphas), dtype=X.dtype)
    test_eta = np.empty((n_samples_test, n_alphas), dtype=X.dtype)

    if sample_weight is None:
        sample_weight = np.ones(X.shape[0])

    model.__setattr__("warm_start", True)
    model.__setattr__("l1_ratio", l1_ratio)

    for i, alpha in enumerate(alphas):
        model.__setattr__("alpha", alpha)
        model.fit(X, y)

        coefs[..., i] = model.coef_
        train_eta[..., i] = model.predict(X_train)
        test_eta[..., i] = model.predict(X_test)

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

        cv = 5 if cv is None else cv
        if isinstance(cv, numbers.Integral):
            self.cv = StratifiedKFold(cv)
        elif isinstance(cv, Iterable):
            self.cv = _CVIterableWrapper(cv)
        elif hasattr(cv, "split"):
            self.cv = cv
        else:
            raise ValueError(
                "Expected cv to be an integer, sklearn model selection object or an iterable"
            )

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
        time: np.array
        event: np.array
        time, event = inverse_transform_survival(y=y)
        sorted_indices: np.array = np.argsort(a=time, kind="stable")
        time_sorted: np.array = time[sorted_indices]
        event_sorted: np.array = event[sorted_indices]
        X_sorted: np.array = X[sorted_indices, :]
        y_sorted: np.array = y[sorted_indices]
        self._validate_params()

        check_consistent_length(X, y)
        Xy = None

        if isinstance(sample_weight, numbers.Number):
            sample_weight = None
        if sample_weight is not None:
            sample_weight = _check_sample_weight(sample_weight, X_sorted, dtype=X.dtype)

        # X, y = _set_order(X, y, order="F")

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

        gradient, hessian = model.gradient(
            linear_predictor=np.zeros(X_sorted.shape[0]),
            time=time_sorted,
            event=event_sorted,
        )
        if alphas is None:
            alphas = [
                _alpha_grid(
                    X_sorted,
                    y_sorted,
                    Xy=None,
                    eps=self.eps,
                    n_alphas=self.n_alphas,
                    gradient=gradient,
                    hessian=hessian,
                    l1_ratio=l1_ratio,
                )
                for l1_ratio in l1_ratios
            ]
        else:

            for index, alpha in enumerate(alphas):
                check_scalar_alpha(alpha, f"alphas[{index}]")

            alphas = np.tile(np.sort(alphas)[::-1], (n_l1_ratios, 1))

        n_alphas = len(alphas[0])
        path_params.update({"n_alphas": n_alphas})

        folds = list(self.cv.split(X, y))
        best_pl_score = 0.0

        jobs = (
            delayed(alpha_path_eta)(
                X,
                y,
                Xy,
                model,
                sample_weight,
                train,
                test,
                alphas=this_alphas,
                l1_ratio=this_l1_ratio,
                eps=self.eps,
            )
            for this_l1_ratio, this_alphas in zip(l1_ratios, alphas)
            for train, test in folds
        )

        eta_path = Parallel(
            n_jobs=self.n_jobs,
            verbose=self.verbose,
            prefer="threads",
        )(jobs)

        train_xb, test_xb, train_y, test_y = zip(*xcoefs_path)
        n_folds = int(len(train_xb) / len(l1_ratios))
        mean_cv_score_l1 = []
        mean_cv_score = []
        self.coef_ = np.zeros(X.shape[1])

        for i in range(len(l1_ratios)):

            train_eta = train_eta_folds[n_folds * i : n_folds * (i + 1)]
            test_eta = test_eta_folds[n_folds * i : n_folds * (i + 1)]
            train_y = train_y_folds[n_folds * i : n_folds * (i + 1)]
            test_y = test_y_folds[n_folds * i : n_folds * (i + 1)]

            if self.cv_score_method == "linear_predictor":
                train_eta_method = np.concatenate(train_eta)
                test_eta_method = np.concatenate(test_eta)
                train_y_method = np.concatenate(train_y)
                test_y_method = np.concatenate(test_y)
                train_time, train_event = inverse_transform_survival(train_y_method)
                test_time, test_event = inverse_transform_survival(test_y_method)

                for j in range(len(alphas[i])):

                    # pass model.loss to do model.loss
                    likelihood = CVSCORERFACTORY[self.cv_score_method](
                        test_eta_method[:, j], test_time, test_event, model.loss
                    )
                    mean_cv_score_l1.append(likelihood)

                mean_cv_score.append(mean_cv_score_l1)

            else:
                test_fold_likelihoods = []
                for k in range(n_folds):
                    train_eta_method = train_eta[k]
                    test_eta_method = test_eta[k]
                    train_y_method = train_y[k]
                    test_y_method = test_y[k]

                    train_time, train_event = inverse_transform_survival(train_y_method)
                    test_time, test_event = inverse_transform_survival(test_y_method)
                    for j in range(len(alphas[i])):
                        fold_likelihood = CVSCORERFACTORY[self.cv_score_method](
                            test_eta_method[:, j],
                            test_time,
                            test_event,
                            train_eta_method[:, j],
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
