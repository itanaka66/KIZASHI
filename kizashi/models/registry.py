"""Optional MLflow Model Registry integration for Layer 4 failure models.

Provides version tracking, metric recording, and Production promotion.

MLflow is an optional dependency. All functions that need it raise
``RuntimeError`` with installation instructions when it is absent.

Promotion flow
--------------
1. A new model is trained and evaluated (walk-forward result).
2. ``log_run()`` records the run in MLflow with metrics and the serialised
   pipeline artifact.
3. ``promote_if_better()`` compares the new model against the current
   "Production" model. If the new model wins on precision *and* recall, it is
   moved to Production and the previous Production model is archived.
4. In shadow mode, the model is only registered in "Staging"; it is not
   promoted automatically — a human (or a CI job) reviews the comparison and
   calls ``promote_to_production()`` manually.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _require_mlflow():
    try:
        import mlflow  # noqa: PLC0415
        return mlflow
    except ImportError as exc:
        raise RuntimeError(
            "mlflow is required for model registry integration. "
            "Install it with: pip install 'kizashi[mlflow]'"
        ) from exc


@dataclass
class RunInfo:
    run_id: str
    model_name: str
    version: str


class ModelRegistry:
    """Thin wrapper around the MLflow tracking and model registry APIs.

    Parameters
    ----------
    tracking_uri : MLflow tracking server URI (e.g. ``http://mlflow:5000``).
        Defaults to the environment variable ``MLFLOW_TRACKING_URI`` or a
        local ``./mlruns`` directory.
    experiment_name : MLflow experiment name; created on first use.
    """

    def __init__(
        self,
        tracking_uri: str | None = None,
        experiment_name: str = "kizashi-failure-prediction",
    ) -> None:
        self._tracking_uri = tracking_uri
        self._experiment_name = experiment_name

    def _mlflow(self):
        mlflow = _require_mlflow()
        if self._tracking_uri:
            mlflow.set_tracking_uri(self._tracking_uri)
        mlflow.set_experiment(self._experiment_name)
        return mlflow

    def _model_name(self, failure_mode: str, horizon_label: str) -> str:
        return f"kizashi-{failure_mode}-{horizon_label}"

    def log_run(
        self,
        meta: Any,           # FailureModelMeta
        pipeline: Any,       # calibrated LGBMClassifier pipeline
        pipeline_path: Path | None = None,
    ) -> RunInfo:
        """Log a training run and register the model in MLflow.

        The model is registered under the name ``kizashi-{failure_mode}-{horizon_label}``.
        The initial stage is ``"None"`` (staging); call ``promote_if_better`` to advance it.

        Returns ``RunInfo`` with the MLflow ``run_id`` and registered model version.
        """
        mlflow = self._mlflow()
        try:
            import lightgbm  # noqa: PLC0415
            import mlflow.lightgbm as mlflow_lgbm  # noqa: PLC0415
        except ImportError:
            mlflow_lgbm = None

        model_name = self._model_name(meta.failure_mode, meta.horizon_label)

        with mlflow.start_run(run_name=meta.version) as run:
            mlflow.set_tags({
                "failure_mode": meta.failure_mode,
                "horizon_label": meta.horizon_label,
                "is_shadow": str(meta.is_shadow),
                "kizashi_version": meta.version,
            })
            mlflow.log_metrics({
                "precision_at_05": meta.precision_at_05,
                "recall": meta.recall,
                "lead_time_median_h": meta.lead_time_median_h,
                "n_pos": float(meta.n_pos),
                "n_neg_sampled": float(meta.n_neg_sampled),
                "psi": meta.psi,
            })

            # Log model artifact
            from kizashi.explain import _extract_lgbm  # noqa: PLC0415
            lgbm_clf = _extract_lgbm(pipeline)
            if mlflow_lgbm is not None:
                model_info = mlflow_lgbm.log_model(lgbm_clf, artifact_path="model")
                mv = mlflow.register_model(
                    model_uri=model_info.model_uri,
                    name=model_name,
                )
            else:
                # Fallback: log as a generic artifact using joblib
                import tempfile, joblib, os  # noqa: PLC0415
                with tempfile.TemporaryDirectory() as tmp:
                    pkl_path = os.path.join(tmp, "pipeline.pkl")
                    joblib.dump(pipeline, pkl_path)
                    mlflow.log_artifact(pkl_path, artifact_path="model")
                mv = None

            run_id = run.info.run_id

        version_str = str(mv.version) if mv is not None else "unknown"
        logger.info(
            "mlflow: logged run=%s model=%s version=%s prec=%.2f rec=%.2f",
            run_id,
            model_name,
            version_str,
            meta.precision_at_05,
            meta.recall,
        )
        return RunInfo(run_id=run_id, model_name=model_name, version=version_str)

    def promote_if_better(
        self,
        meta: Any,       # FailureModelMeta of the candidate
        run_info: RunInfo,
    ) -> bool:
        """Promote the candidate to Production if it outperforms the current model.

        Promotion criteria: candidate precision ≥ current AND candidate recall ≥ current.
        The previous Production model is transitioned to Archived.

        Returns True if the model was promoted.
        """
        if meta.is_shadow:
            logger.info("mlflow: shadow model %s not auto-promoted", run_info.model_name)
            return False

        mlflow = self._mlflow()
        client = mlflow.tracking.MlflowClient()
        model_name = run_info.model_name

        # Find current Production version
        try:
            prod_versions = client.get_latest_versions(model_name, stages=["Production"])
        except Exception:
            prod_versions = []

        # Retrieve current production metrics for comparison
        should_promote = True
        if prod_versions:
            prod_run_id = prod_versions[0].run_id
            try:
                prod_metrics = client.get_run(prod_run_id).data.metrics
                prod_prec = prod_metrics.get("precision_at_05", 0.0)
                prod_rec = prod_metrics.get("recall", 0.0)
                should_promote = (
                    meta.precision_at_05 >= prod_prec and meta.recall >= prod_rec
                )
                if not should_promote:
                    logger.info(
                        "mlflow: candidate (prec=%.2f, rec=%.2f) does not outperform "
                        "production (prec=%.2f, rec=%.2f); keeping current model",
                        meta.precision_at_05,
                        meta.recall,
                        prod_prec,
                        prod_rec,
                    )
            except Exception as exc:
                logger.warning("mlflow: could not fetch production metrics: %s", exc)

        if should_promote:
            # Archive previous Production
            for pv in prod_versions:
                client.transition_model_version_stage(model_name, pv.version, "Archived")
            # Promote candidate
            client.transition_model_version_stage(model_name, run_info.version, "Production")
            logger.info(
                "mlflow: promoted %s v%s to Production", model_name, run_info.version
            )
        return should_promote

    def promote_to_production(self, model_name: str, version: str) -> None:
        """Unconditionally promote a registered model version to Production."""
        mlflow = self._mlflow()
        client = mlflow.tracking.MlflowClient()
        for pv in client.get_latest_versions(model_name, stages=["Production"]):
            client.transition_model_version_stage(model_name, pv.version, "Archived")
        client.transition_model_version_stage(model_name, version, "Production")
        logger.info("mlflow: promoted %s v%s to Production (manual)", model_name, version)

    def get_production_run_id(self, failure_mode: str, horizon_label: str) -> str | None:
        """Return the run_id of the current Production model, or None."""
        mlflow = self._mlflow()
        client = mlflow.tracking.MlflowClient()
        model_name = self._model_name(failure_mode, horizon_label)
        try:
            versions = client.get_latest_versions(model_name, stages=["Production"])
            return versions[0].run_id if versions else None
        except Exception:
            return None


class NoOpRegistry:
    """Drop-in replacement when MLflow is disabled or unavailable."""

    def log_run(self, meta: Any, pipeline: Any, pipeline_path: Any = None) -> None:
        return None

    def promote_if_better(self, meta: Any, run_info: Any) -> bool:
        return False

    def promote_to_production(self, model_name: str, version: str) -> None:
        pass

    def get_production_run_id(self, failure_mode: str, horizon_label: str) -> None:
        return None
