from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, precision_score, recall_score
from xgboost import XGBClassifier, XGBRegressor

from paperfrog.domain.types import SafeError
from paperfrog.features.engineering import CORE_FEATURES, SCHEMA_HASH

PARAMS = dict(
    max_depth=3,
    learning_rate=0.03,
    n_estimators=2000,
    min_child_weight=20,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_lambda=10,
    reg_alpha=0.2,
    early_stopping_rounds=100,
    random_state=42,
    n_jobs=2,
    tree_method="hist",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(x, -40, 40)))


def partitions(
    data: pd.DataFrame, test_start: pd.Timestamp, months: int
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    t = pd.to_datetime(data.timestamp_ms, unit="ms", utc=True)
    end = pd.to_datetime(data.label_end_ms, unit="ms", utc=True)
    validation_start = test_start - pd.DateOffset(months=2)
    train_start = validation_start - pd.DateOffset(months=months)
    gap = pd.Timedelta(hours=4)
    train = data[(t >= train_start) & (end < validation_start - gap)]
    validation = data[(t >= validation_start + gap) & (end < test_start - gap)]
    test = data[(t >= test_start + gap) & (t < test_start + pd.DateOffset(months=1))]
    return train, validation, test


def check_samples(data: pd.DataFrame) -> None:
    required = set(
        CORE_FEATURES + ["timestamp_ms", "label_end_ms", "success", "net_return", "episode_id"]
    )
    if not required <= set(data):
        raise SafeError("episode training data missing required columns")
    if data.episode_id.duplicated().any() or not data.timestamp_ms.is_monotonic_increasing:
        raise SafeError("duplicate episodes or unsorted training data")
    if not np.isfinite(data[CORE_FEATURES + ["net_return"]].to_numpy()).all():
        raise SafeError("nonfinite features; do not impute missing core history")
    if (data.label_end_ms < data.timestamp_ms).any() or (
        data.label_end_ms - data.timestamp_ms > 14400000
    ).any():
        raise SafeError("invalid label horizon")


def train_member(train: pd.DataFrame, validation: pd.DataFrame, output: Path) -> dict[str, Any]:
    if len(train) < 100 or len(validation) < 40 or train.success.nunique() != 2:
        raise SafeError("insufficient training/validation episodes or classes")
    middle = int(validation.timestamp_ms.median())
    early = validation[validation.label_end_ms < middle - 14400000]
    calibration = validation[validation.timestamp_ms > middle + 14400000]
    if len(early) < 10 or len(calibration) < 20 or calibration.success.nunique() != 2:
        raise SafeError("insufficient disjoint early-stop/calibration partitions")
    x = train[CORE_FEATURES].to_numpy()
    low, high = np.quantile(x, [0.005, 0.995], axis=0)
    center = np.median(x, axis=0)
    scale = np.maximum(np.std(np.clip(x, low, high), axis=0), 1e-10)

    def transform(frame: pd.DataFrame) -> np.ndarray:
        return (np.clip(frame[CORE_FEATURES].to_numpy(), low, high) - center) / scale

    classifier = XGBClassifier(**PARAMS, objective="binary:logistic", eval_metric="logloss")
    regressor = XGBRegressor(**PARAMS, objective="reg:squarederror", eval_metric="rmse")
    classifier.fit(
        transform(train), train.success, eval_set=[(transform(early), early.success)], verbose=False
    )
    regressor.fit(
        transform(train),
        train.net_return,
        eval_set=[(transform(early), early.net_return)],
        verbose=False,
    )
    raw = classifier.predict_proba(transform(calibration))[:, 1]
    logits = np.log(np.clip(raw, 1e-6, 1 - 1e-6) / (1 - np.clip(raw, 1e-6, 1 - 1e-6))).reshape(
        -1, 1
    )
    platt = LogisticRegression(C=1).fit(logits, calibration.success)
    baseline = LogisticRegression(C=0.1, max_iter=1000).fit(transform(train), train.success)
    output.mkdir(parents=True, exist_ok=False)
    classifier.save_model(output / "classifier.json")
    regressor.save_model(output / "regressor.json")
    metadata = {
        "low": low.tolist(),
        "high": high.tolist(),
        "center": center.tolist(),
        "scale": scale.tolist(),
        "calibration_coefficient": float(platt.coef_[0, 0]),
        "calibration_intercept": float(platt.intercept_[0]),
        "baseline_coefficients": baseline.coef_[0].tolist(),
        "baseline_intercept": float(baseline.intercept_[0]),
        "train_rows": len(train),
        "validation_rows": len(validation),
        "calibration_rows": len(calibration),
        "train_end_ms": int(train.label_end_ms.max()),
        "validation_end_ms": int(validation.label_end_ms.max()),
        "classifier_best_iteration": classifier.best_iteration,
        "regressor_best_iteration": regressor.best_iteration,
    }
    (output / "preprocessing.json").write_text(json.dumps(metadata, indent=2))
    return metadata


class Ensemble:
    def __init__(self, directory: Path, max_age_days: int = 30, now: datetime | None = None):
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            raise SafeError("model artifact missing")
        self.manifest = json.loads(manifest_path.read_text())
        if self.manifest["schema_hash"] != SCHEMA_HASH:
            raise SafeError("model feature-schema mismatch")
        created = datetime.fromisoformat(self.manifest["created_at"])
        age = ((now or datetime.now(UTC)) - created).total_seconds()
        if not 0 <= age <= max_age_days * 86400:
            raise SafeError("model expired or timestamp in future")
        for name, expected in self.manifest["files"].items():
            path = (directory / name).resolve()
            if (
                not path.is_relative_to(directory.resolve())
                or not path.is_file()
                or digest(path) != expected
            ):
                raise SafeError("model artifact integrity failed")
        self.hash = digest(manifest_path)
        self.members = []
        for name in self.manifest["members"]:
            folder = directory / name
            c, r = XGBClassifier(), XGBRegressor()
            c.load_model(folder / "classifier.json")
            r.load_model(folder / "regressor.json")
            p = json.loads((folder / "preprocessing.json").read_text())
            self.members.append((c, r, p))
        if not self.members:
            raise SafeError("empty model ensemble")

    def predict(self, data: pd.DataFrame) -> dict[str, Any]:
        x = data[CORE_FEATURES].to_numpy()
        if not np.isfinite(x).all():
            raise SafeError("missing or nonfinite model features")
        probabilities, returns, baselines = [], [], []
        drift = []
        for c, r, p in self.members:
            outside = ((x < np.array(p["low"])) | (x > np.array(p["high"]))).mean(axis=1)
            drift.append(outside)
            z = (np.clip(x, p["low"], p["high"]) - p["center"]) / p["scale"]
            raw = np.clip(c.predict_proba(z)[:, 1], 1e-6, 1 - 1e-6)
            probabilities.append(
                sigmoid(
                    np.log(raw / (1 - raw)) * p["calibration_coefficient"]
                    + p["calibration_intercept"]
                )
            )
            returns.append(r.predict(z))
            baselines.append(
                sigmoid(z @ np.array(p["baseline_coefficients"]) + p["baseline_intercept"])
            )
        return {
            "probabilities": np.array(probabilities).T,
            "returns": np.array(returns).T,
            "baseline": np.median(baselines, axis=0),
            "severe_drift": np.max(drift, axis=0) > 0.4,
        }


def train_ensemble(data: pd.DataFrame, test_start: pd.Timestamp, output: Path) -> Ensemble:
    check_samples(data)
    if output.exists():
        raise SafeError("artifact directory exists; artifacts are immutable")
    output.mkdir(parents=True)
    members = []
    try:
        for months in (6, 12, 18):
            train, val, _ = partitions(data, test_start, months)
            # Short histories cannot masquerade as an 18-month member.
            if (
                train.empty
                or int(train.timestamp_ms.max() - train.timestamp_ms.min())
                < (months - 1) * 28 * 86400000
            ):
                continue
            name = f"member-{months}m"
            train_member(train, val, output / name)
            members.append(name)
        if not members:
            raise SafeError("insufficient point-in-time history for any ensemble member")
        files = {str(p.relative_to(output)): digest(p) for p in output.rglob("*.json")}
        manifest = {
            "schema_hash": SCHEMA_HASH,
            "created_at": datetime.now(UTC).isoformat(),
            "members": members,
            "files": files,
            "test_start": test_start.isoformat(),
            "params": PARAMS,
            "promotion": "unapproved",
            "training_data_hash": hashlib.sha256(data.to_json().encode()).hexdigest(),
            "historical_llm": False,
            "optional_features_excluded": ["liquidations", "depth"],
            "calibration": "Platt on disjoint chronological validation partition",
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return Ensemble(output)
    except Exception:
        (output / "FAILED").write_text("Training failed. This directory cannot be promoted.\n")
        raise


def walk_forward(data: pd.DataFrame, output: Path) -> pd.DataFrame:
    check_samples(data)
    first = pd.to_datetime(data.timestamp_ms.min(), unit="ms", utc=True) + pd.DateOffset(months=9)
    first = first.normalize().replace(day=1)
    last = pd.to_datetime(data.timestamp_ms.max(), unit="ms", utc=True)
    chunks = []
    for start in pd.date_range(first, last, freq="MS"):
        if start + pd.DateOffset(months=1) > last:
            break
        model = train_ensemble(data, start, output / start.strftime("%Y-%m"))
        _, _, test = partitions(data, start, 12)
        if test.empty:
            continue
        prediction = model.predict(test)
        result = test.copy()
        result["probability"] = np.median(prediction["probabilities"], axis=1)
        result["predicted_return"] = np.median(prediction["returns"], axis=1)
        result["prediction_std"] = np.std(prediction["returns"], axis=1)
        result["baseline_probability"] = prediction["baseline"]
        result["severe_drift"] = prediction["severe_drift"]
        result["model_hash"] = model.hash
        result["out_of_sample"] = True
        chunks.append(result)
    if not chunks:
        raise SafeError("no complete out-of-sample months available")
    result = pd.concat(chunks, ignore_index=True)
    (output / "evaluation.json").write_text(json.dumps(evaluate(result), indent=2))
    result.to_parquet(output / "out_of_sample.parquet", index=False)
    return result


def evaluate(predictions: pd.DataFrame) -> dict[str, Any]:
    if predictions.empty:
        raise SafeError("no predictions to evaluate")
    y, p = predictions.success, predictions.probability
    return {
        "samples": len(predictions),
        "brier": float(brier_score_loss(y, p)),
        "precision": float(precision_score(y, p >= 0.75, zero_division=0)),
        "recall": float(recall_score(y, p >= 0.75, zero_division=0)),
        "calibration_bins": [
            {
                "lower": i / 10,
                "count": int(((p >= i / 10) & (p < (i + 1) / 10)).sum()),
                "mean_probability": float(p[(p >= i / 10) & (p < (i + 1) / 10)].mean())
                if ((p >= i / 10) & (p < (i + 1) / 10)).any()
                else None,
                "observed": float(y[(p >= i / 10) & (p < (i + 1) / 10)].mean())
                if ((p >= i / 10) & (p < (i + 1) / 10)).any()
                else None,
            }
            for i in range(10)
        ],
    }
