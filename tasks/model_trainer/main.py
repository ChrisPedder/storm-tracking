"""Train a LightGBM classifier for severe storm prediction."""

import io
import json
import logging
import os
import sys
import tempfile

import boto3
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import f1_score, precision_score, recall_score, roc_auc_score

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("model-trainer")

S3_BUCKET = os.environ["S3_BUCKET"]
INPUT_PREFIX = os.environ.get("INPUT_PREFIX", "output/")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "output/model/")

META_COLUMNS = {"event_datetime", "grid_lat", "grid_lon", "label", "flash_count"}

s3 = boto3.client("s3")


def load_parquet(key: str) -> pd.DataFrame:
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return pd.read_parquet(io.BytesIO(resp["Body"].read()))


def compute_metrics(y_true: np.ndarray, y_proba: np.ndarray, threshold: float = 0.5) -> dict:
    y_pred = (y_proba >= threshold).astype(int)
    return {
        "auc": float(roc_auc_score(y_true, y_proba)) if len(np.unique(y_true)) > 1 else 0.0,
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "n_samples": int(len(y_true)),
        "n_positive": int(y_true.sum()),
    }


def main() -> None:
    logger.info("Model trainer starting")
    logger.info("  S3 bucket: %s", S3_BUCKET)
    logger.info("  Input prefix: %s", INPUT_PREFIX)
    logger.info("  Output prefix: %s", OUTPUT_PREFIX)

    train = load_parquet(f"{INPUT_PREFIX}train.parquet")
    val = load_parquet(f"{INPUT_PREFIX}val.parquet")
    test = load_parquet(f"{INPUT_PREFIX}test.parquet")

    logger.info("Loaded train=%d, val=%d, test=%d", len(train), len(val), len(test))

    feature_cols = [c for c in train.columns if c not in META_COLUMNS]
    logger.info("Using %d features", len(feature_cols))

    X_train, y_train = train[feature_cols], train["label"]
    X_val, y_val = val[feature_cols], val["label"]
    X_test, y_test = test[feature_cols], test["label"]

    train_data = lgb.Dataset(X_train, label=y_train)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

    params = {
        "objective": "binary",
        "metric": "auc",
        "is_unbalance": True,
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
        "seed": 42,
    }

    logger.info("Training LightGBM with params: %s", params)
    model = lgb.train(
        params,
        train_data,
        num_boost_round=1000,
        valid_sets=[val_data],
        callbacks=[
            lgb.early_stopping(stopping_rounds=50),
            lgb.log_evaluation(period=25),
        ],
    )

    logger.info("Training complete after %d rounds", model.best_iteration)

    train_metrics = compute_metrics(y_train.values, model.predict(X_train))
    val_metrics = compute_metrics(y_val.values, model.predict(X_val))
    test_metrics = compute_metrics(y_test.values, model.predict(X_test))

    logger.info("Train: AUC=%.4f  F1=%.4f  P=%.4f  R=%.4f",
                train_metrics["auc"], train_metrics["f1"],
                train_metrics["precision"], train_metrics["recall"])
    logger.info("Val:   AUC=%.4f  F1=%.4f  P=%.4f  R=%.4f",
                val_metrics["auc"], val_metrics["f1"],
                val_metrics["precision"], val_metrics["recall"])
    logger.info("Test:  AUC=%.4f  F1=%.4f  P=%.4f  R=%.4f",
                test_metrics["auc"], test_metrics["f1"],
                test_metrics["precision"], test_metrics["recall"])

    if test_metrics["auc"] < 0.5:
        logger.error("Test AUC %.4f is worse than random — failing", test_metrics["auc"])
        sys.exit(1)

    importance = pd.DataFrame({
        "feature": feature_cols,
        "gain": model.feature_importance(importance_type="gain"),
        "split": model.feature_importance(importance_type="split"),
    }).sort_values("gain", ascending=False)

    logger.info("Top 10 features by gain:\n%s", importance.head(10).to_string(index=False))

    with tempfile.NamedTemporaryFile(suffix=".lgb", delete=False) as tmp:
        model.save_model(tmp.name)
        with open(tmp.name, "rb") as f:
            s3.put_object(Bucket=S3_BUCKET, Key=f"{OUTPUT_PREFIX}model.lgb", Body=f.read())
    os.unlink(tmp.name)
    logger.info("Uploaded model to s3://%s/%smodel.lgb", S3_BUCKET, OUTPUT_PREFIX)

    metrics = {
        "best_iteration": model.best_iteration,
        "params": params,
        "train": train_metrics,
        "val": val_metrics,
        "test": test_metrics,
    }
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{OUTPUT_PREFIX}metrics.json",
        Body=json.dumps(metrics, indent=2).encode(),
    )
    logger.info("Uploaded metrics to s3://%s/%smetrics.json", S3_BUCKET, OUTPUT_PREFIX)

    imp_records = importance.to_dict(orient="records")
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{OUTPUT_PREFIX}feature_importance.json",
        Body=json.dumps(imp_records, indent=2).encode(),
    )

    logger.info("Model trainer complete")


if __name__ == "__main__":
    main()
