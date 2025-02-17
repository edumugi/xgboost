"""Tests for updaters."""
import json
from functools import partial, update_wrapper
from typing import Any, Dict

import numpy as np

import xgboost as xgb
import xgboost.testing as tm


def get_basescore(model: xgb.XGBModel) -> float:
    """Get base score from an XGBoost sklearn estimator."""
    base_score = float(
        json.loads(model.get_booster().save_config())["learner"]["learner_model_param"][
            "base_score"
        ]
    )
    return base_score


def check_init_estimation(tree_method: str) -> None:
    """Test for init estimation."""
    from sklearn.datasets import (
        make_classification,
        make_multilabel_classification,
        make_regression,
    )

    def run_reg(X: np.ndarray, y: np.ndarray) -> None:  # pylint: disable=invalid-name
        reg = xgb.XGBRegressor(tree_method=tree_method, max_depth=1, n_estimators=1)
        reg.fit(X, y, eval_set=[(X, y)])
        base_score_0 = get_basescore(reg)
        score_0 = reg.evals_result()["validation_0"]["rmse"][0]

        reg = xgb.XGBRegressor(
            tree_method=tree_method, max_depth=1, n_estimators=1, boost_from_average=0
        )
        reg.fit(X, y, eval_set=[(X, y)])
        base_score_1 = get_basescore(reg)
        score_1 = reg.evals_result()["validation_0"]["rmse"][0]
        assert not np.isclose(base_score_0, base_score_1)
        assert score_0 < score_1  # should be better

    # pylint: disable=unbalanced-tuple-unpacking
    X, y = make_regression(n_samples=4096, random_state=17)
    run_reg(X, y)
    # pylint: disable=unbalanced-tuple-unpacking
    X, y = make_regression(n_samples=4096, n_targets=3, random_state=17)
    run_reg(X, y)

    def run_clf(X: np.ndarray, y: np.ndarray) -> None:  # pylint: disable=invalid-name
        clf = xgb.XGBClassifier(tree_method=tree_method, max_depth=1, n_estimators=1)
        clf.fit(X, y, eval_set=[(X, y)])
        base_score_0 = get_basescore(clf)
        score_0 = clf.evals_result()["validation_0"]["logloss"][0]

        clf = xgb.XGBClassifier(
            tree_method=tree_method, max_depth=1, n_estimators=1, boost_from_average=0
        )
        clf.fit(X, y, eval_set=[(X, y)])
        base_score_1 = get_basescore(clf)
        score_1 = clf.evals_result()["validation_0"]["logloss"][0]
        assert not np.isclose(base_score_0, base_score_1)
        assert score_0 < score_1  # should be better

    # pylint: disable=unbalanced-tuple-unpacking
    X, y = make_classification(n_samples=4096, random_state=17)
    run_clf(X, y)
    X, y = make_multilabel_classification(
        n_samples=4096, n_labels=3, n_classes=5, random_state=17
    )
    run_clf(X, y)


# pylint: disable=too-many-locals
def check_quantile_loss(tree_method: str, weighted: bool) -> None:
    """Test for quantile loss."""
    from sklearn.datasets import make_regression
    from sklearn.metrics import mean_pinball_loss

    from xgboost.sklearn import _metric_decorator

    n_samples = 4096
    n_features = 8
    n_estimators = 8
    # non-zero base score can cause floating point difference with GPU predictor.
    # multi-class has small difference than single target in the prediction kernel
    base_score = 0.0
    rng = np.random.RandomState(1994)
    # pylint: disable=unbalanced-tuple-unpacking
    X, y = make_regression(
        n_samples=n_samples,
        n_features=n_features,
        random_state=rng,
    )
    if weighted:
        weight = rng.random(size=n_samples)
    else:
        weight = None

    Xy = xgb.QuantileDMatrix(X, y, weight=weight)

    alpha = np.array([0.1, 0.5])
    evals_result: Dict[str, Dict] = {}
    booster_multi = xgb.train(
        {
            "objective": "reg:quantileerror",
            "tree_method": tree_method,
            "quantile_alpha": alpha,
            "base_score": base_score,
        },
        Xy,
        num_boost_round=n_estimators,
        evals=[(Xy, "Train")],
        evals_result=evals_result,
    )
    predt_multi = booster_multi.predict(Xy, strict_shape=True)

    assert tm.non_increasing(evals_result["Train"]["quantile"])
    assert evals_result["Train"]["quantile"][-1] < 20.0
    # check that there's a way to use custom metric and compare the results.
    metrics = [
        _metric_decorator(
            update_wrapper(
                partial(mean_pinball_loss, sample_weight=weight, alpha=alpha[i]),
                mean_pinball_loss,
            )
        )
        for i in range(alpha.size)
    ]

    predts = np.empty(predt_multi.shape)
    for i in range(alpha.shape[0]):
        a = alpha[i]

        booster_i = xgb.train(
            {
                "objective": "reg:quantileerror",
                "tree_method": tree_method,
                "quantile_alpha": a,
                "base_score": base_score,
            },
            Xy,
            num_boost_round=n_estimators,
            evals=[(Xy, "Train")],
            custom_metric=metrics[i],
            evals_result=evals_result,
        )
        assert tm.non_increasing(evals_result["Train"]["quantile"])
        assert evals_result["Train"]["quantile"][-1] < 30.0
        np.testing.assert_allclose(
            np.array(evals_result["Train"]["quantile"]),
            np.array(evals_result["Train"]["mean_pinball_loss"]),
            atol=1e-6,
            rtol=1e-6,
        )
        predts[:, i] = booster_i.predict(Xy)

    for i in range(alpha.shape[0]):
        np.testing.assert_allclose(predts[:, i], predt_multi[:, i])


def check_cut(
    n_entries: int, indptr: np.ndarray, data: np.ndarray, dtypes: Any
) -> None:
    """Check the cut values."""
    from pandas.api.types import is_categorical_dtype

    assert data.shape[0] == indptr[-1]
    assert data.shape[0] == n_entries

    assert indptr.dtype == np.uint64
    for i in range(1, indptr.size):
        beg = int(indptr[i - 1])
        end = int(indptr[i])
        for j in range(beg + 1, end):
            assert data[j] > data[j - 1]
            if is_categorical_dtype(dtypes[i - 1]):
                assert data[j] == data[j - 1] + 1


def check_get_quantile_cut_device(tree_method: str, use_cupy: bool) -> None:
    """Check with optional cupy."""
    from pandas.api.types import is_categorical_dtype

    n_samples = 1024
    n_features = 14
    max_bin = 16
    dtypes = [np.float32] * n_features

    # numerical
    X, y, w = tm.make_regression(n_samples, n_features, use_cupy=use_cupy)
    # - qdm
    Xyw: xgb.DMatrix = xgb.QuantileDMatrix(X, y, weight=w, max_bin=max_bin)
    indptr, data = Xyw.get_quantile_cut()
    check_cut((max_bin + 1) * n_features, indptr, data, dtypes)
    # - dm
    Xyw = xgb.DMatrix(X, y, weight=w)
    xgb.train({"tree_method": tree_method, "max_bin": max_bin}, Xyw)
    indptr, data = Xyw.get_quantile_cut()
    check_cut((max_bin + 1) * n_features, indptr, data, dtypes)
    # - ext mem
    n_batches = 3
    n_samples_per_batch = 256
    it = tm.IteratorForTest(
        *tm.make_batches(n_samples_per_batch, n_features, n_batches, use_cupy),
        cache="cache",
    )
    Xy: xgb.DMatrix = xgb.DMatrix(it)
    xgb.train({"tree_method": tree_method, "max_bin": max_bin}, Xyw)
    indptr, data = Xyw.get_quantile_cut()
    check_cut((max_bin + 1) * n_features, indptr, data, dtypes)

    # categorical
    n_categories = 32
    X, y = tm.make_categorical(n_samples, n_features, n_categories, False, sparsity=0.8)
    if use_cupy:
        import cudf  # pylint: disable=import-error
        import cupy as cp  # pylint: disable=import-error

        X = cudf.from_pandas(X)
        y = cp.array(y)
    # - qdm
    Xy = xgb.QuantileDMatrix(X, y, max_bin=max_bin, enable_categorical=True)
    indptr, data = Xy.get_quantile_cut()
    check_cut(n_categories * n_features, indptr, data, X.dtypes)
    # - dm
    Xy = xgb.DMatrix(X, y, enable_categorical=True)
    xgb.train({"tree_method": tree_method, "max_bin": max_bin}, Xy)
    indptr, data = Xy.get_quantile_cut()
    check_cut(n_categories * n_features, indptr, data, X.dtypes)

    # mixed
    X, y = tm.make_categorical(
        n_samples, n_features, n_categories, False, sparsity=0.8, cat_ratio=0.5
    )
    n_cat_features = len([0 for dtype in X.dtypes if is_categorical_dtype(dtype)])
    n_num_features = n_features - n_cat_features
    n_entries = n_categories * n_cat_features + (max_bin + 1) * n_num_features
    # - qdm
    Xy = xgb.QuantileDMatrix(X, y, max_bin=max_bin, enable_categorical=True)
    indptr, data = Xy.get_quantile_cut()
    check_cut(n_entries, indptr, data, X.dtypes)
    # - dm
    Xy = xgb.DMatrix(X, y, enable_categorical=True)
    xgb.train({"tree_method": tree_method, "max_bin": max_bin}, Xy)
    indptr, data = Xy.get_quantile_cut()
    check_cut(n_entries, indptr, data, X.dtypes)


def check_get_quantile_cut(tree_method: str) -> None:
    """Check the quantile cut getter."""

    use_cupy = tree_method == "gpu_hist"
    check_get_quantile_cut_device(tree_method, False)
    if use_cupy:
        check_get_quantile_cut_device(tree_method, True)
