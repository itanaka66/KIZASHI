"""Command line entry point: `kizashi serve` and `kizashi train`."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from prometheus_client import REGISTRY, start_http_server

from kizashi.config import ConfigError, load_config
from kizashi.exporter import SnapshotCollector
from kizashi.inference import InferenceEngine
from kizashi.models.store import ModelStore
from kizashi.sources.alertmanager import AlertmanagerClient, AlertmanagerError
from kizashi.sources.prometheus import PrometheusClient, PrometheusError
from kizashi.topology import TopologyStore
from kizashi.trainer import train

log = logging.getLogger("kizashi")


def _serve(args) -> int:
    config = load_config(args.config)
    client = PrometheusClient(
        config.prometheus.url, config.prometheus.timeout, config.prometheus.bearer_token_file
    )
    collector = SnapshotCollector()
    REGISTRY.register(collector)
    engine = InferenceEngine(config, client, ModelStore(config.model_dir), collector, REGISTRY)

    if args.once:
        for s in engine.run_once():
            labels = ",".join(f'{k}="{v}"' for k, v in sorted(s.labels.items()))
            print(f"{s.name}{{{labels}}} {s.value:g}")
        return 0

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    start_http_server(config.listen_port, config.listen_address)
    log.info("serving /metrics on %s:%d", config.listen_address, config.listen_port)
    engine.run_forever(stop)
    client.close()
    return 0


def _train(args) -> int:
    config = load_config(args.config)
    client = PrometheusClient(
        config.prometheus.long_term_url, config.prometheus.timeout, config.prometheus.bearer_token_file
    )
    store = ModelStore(config.model_dir)
    topology = TopologyStore(config.model_dir) if not args.no_topology else None

    # Fetch silences from Alertmanager when configured.
    silences = None
    if config.alertmanager:
        am_client = AlertmanagerClient(config.alertmanager.url)
        try:
            silences = am_client.active_silences()
            if silences:
                log.info("excluding %d active silence(s) from training data", len(silences))
        except AlertmanagerError as e:
            log.warning("could not fetch silences from Alertmanager (%s); proceeding without exclusion", e)
        finally:
            am_client.close()

    try:
        fitted = train(config, client, store, silences=silences, topology=topology)
    finally:
        client.close()
    for metric, n in fitted.items():
        print(f"{metric}: {n} series")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kizashi", description="AI failure prediction for Prometheus")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the inference loop and expose /metrics")
    serve.add_argument("--config", "-c", default="kizashi.yaml")
    serve.add_argument("--once", action="store_true", help="run one cycle, print results, and exit")
    serve.set_defaults(func=_serve)

    tr = sub.add_parser("train", help="train models from long-term storage")
    tr.add_argument("--config", "-c", default="kizashi.yaml")
    tr.add_argument(
        "--no-topology",
        action="store_true",
        help="skip topology change detection and store update",
    )
    tr.set_defaults(func=_train)

    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request at INFO, which floods the log at one query per target per cycle.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return args.func(args)
    except (ConfigError, PrometheusError) as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
