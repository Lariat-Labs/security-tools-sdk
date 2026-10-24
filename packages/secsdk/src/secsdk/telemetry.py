"""OpenTelemetry tracing and metrics for secsdk (optional).

The SDK depends only on ``opentelemetry-api``. If it isn't installed, or no
exporter is configured, every hook here is a cheap no-op.

What is recorded:

* **Spans**: one per operation (``secsdk kev.lookup``) with nested spans for
  composite operations, and one per HTTP request (``GET www.cisa.gov``).
* **Metrics**: operation calls and duration, HTTP requests, duration, retries
  and cache hits.

Parameter *values* are not recorded unless ``SECSDK_TELEMETRY_CAPTURE_PARAMS=true``
(domains and KQL queries can be sensitive). Query strings are never recorded.

Applications choose the destination with :func:`configure`, driven by environment
variables (first match wins):

=========================================  ==============================================
``APPLICATIONINSIGHTS_CONNECTION_STRING``  Azure Monitor / Application Insights
``OTEL_EXPORTER_OTLP_ENDPOINT``            Any OTLP backend or OpenTelemetry Collector
``SECSDK_TELEMETRY_FILE``                  JSON lines file (one span per line)
=========================================  ==============================================
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import time
import warnings
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from . import __version__

try:  # pragma: no cover - exercised implicitly by import
    from opentelemetry import context as otel_context
    from opentelemetry import metrics as otel_metrics
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    AVAILABLE = True
except ImportError:  # opentelemetry-api not installed
    AVAILABLE = False

NAME = "secsdk"
_configured = False
_instruments: dict[str, Any] = {}


def enabled() -> bool:
    return AVAILABLE and os.environ.get("SECSDK_TELEMETRY", "").lower() not in ("0", "false", "off")


def _capture_params() -> bool:
    return os.environ.get("SECSDK_TELEMETRY_CAPTURE_PARAMS", "").lower() in ("1", "true", "yes")


# ----- instruments ----------------------------------------------------------------------------


def _instrument(kind: str, name: str, unit: str, description: str) -> Any:
    inst = _instruments.get(name)
    if inst is None:
        meter = otel_metrics.get_meter(NAME, __version__)
        factory = meter.create_counter if kind == "counter" else meter.create_histogram
        inst = factory(name, unit=unit, description=description)
        _instruments[name] = inst
    return inst


def _record_operation(attrs: dict[str, Any], seconds: float) -> None:
    _instrument("counter", "secsdk.operation.calls", "{call}", "Operations executed").add(1, attrs)
    _instrument("histogram", "secsdk.operation.duration", "s", "Operation duration").record(
        seconds, attrs
    )


def _record_http(attrs: dict[str, Any], seconds: float, retries: int) -> None:
    _instrument("counter", "secsdk.http.requests", "{request}", "Upstream HTTP requests").add(
        1, attrs
    )
    _instrument("histogram", "secsdk.http.duration", "s", "Upstream HTTP request duration").record(
        seconds, attrs
    )
    if retries:
        _instrument("counter", "secsdk.http.retries", "{retry}", "Upstream HTTP retries").add(
            retries, attrs
        )


# ----- spans ------------------------------------------------------------------------------------


class _NoopRecorder:
    # HTTP bookkeeping fields, written by the HTTP layer either way.
    retries = 0
    status: int | None = None
    cache_hit = False

    def set(self, key: str, value: Any) -> None:
        pass

    def fail(self, code: str, exc: BaseException | None = None) -> None:
        pass


class _SpanRecorder:
    def __init__(self, span: Any, metric_attrs: dict[str, Any]) -> None:
        self.span = span
        self.metric_attrs = metric_attrs
        self.outcome = "ok"
        self.retries = 0
        self.status: int | None = None
        self.cache_hit = False

    def set(self, key: str, value: Any) -> None:
        if value is not None:
            self.span.set_attribute(key, value)

    def fail(self, code: str, exc: BaseException | None = None) -> None:
        self.outcome = code
        self.span.set_attribute("error.type", code)
        self.span.set_status(Status(StatusCode.ERROR, str(exc) if exc else code))
        if exc is not None:
            self.span.record_exception(exc)


@contextmanager
def operation_span(fqname: str, provider: str, params: Mapping[str, Any]) -> Iterator[Any]:
    """Wrap one SDK operation. Yields a recorder with ``set`` and ``fail``."""
    if not enabled():
        yield _NoopRecorder()
        return
    tracer = otel_trace.get_tracer(NAME, __version__)
    start = time.perf_counter()
    with tracer.start_as_current_span(
        f"secsdk {fqname}", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("secsdk.operation", fqname)
        span.set_attribute("secsdk.provider", provider)
        span.set_attribute("secsdk.params", sorted(params))
        if _capture_params():
            span.set_attribute("secsdk.params.values", json.dumps(params, default=str)[:2000])
        rec = _SpanRecorder(span, {"secsdk.operation": fqname, "secsdk.provider": provider})
        try:
            yield rec
        finally:
            span.set_attribute("secsdk.outcome", rec.outcome)
            _record_operation(
                rec.metric_attrs | {"secsdk.outcome": rec.outcome}, time.perf_counter() - start
            )


@contextmanager
def http_span(method: str, host: str, path: str) -> Iterator[Any]:
    """Wrap one upstream HTTP request (including its retries)."""
    if not enabled():
        yield _NoopRecorder()
        return
    tracer = otel_trace.get_tracer(NAME, __version__)
    start = time.perf_counter()
    with tracer.start_as_current_span(
        f"{method} {host}",
        kind=SpanKind.CLIENT,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("http.request.method", method)
        span.set_attribute("server.address", host)
        span.set_attribute("url.path", path)
        rec = _SpanRecorder(span, {"http.request.method": method, "server.address": host})
        try:
            yield rec
        finally:
            span.set_attribute("http.request.resend_count", rec.retries)
            span.set_attribute("secsdk.cache_hit", rec.cache_hit)
            if rec.status is not None:
                span.set_attribute("http.response.status_code", rec.status)
            attrs = rec.metric_attrs | {
                "http.response.status_code": rec.status or 0,
                "secsdk.cache_hit": rec.cache_hit,
            }
            _record_http(attrs, time.perf_counter() - start, rec.retries)


# ----- context propagation ----------------------------------------------------------------------


def inject_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return ``env`` plus ``TRACEPARENT``/``TRACESTATE`` for the current span.

    Pass the result to a subprocess (e.g. ``secctl``) so its spans join this trace.
    """
    env = dict(os.environ if env is None else env)
    if enabled():
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
        if "traceparent" in carrier:
            env["TRACEPARENT"] = carrier["traceparent"]
        if "tracestate" in carrier:
            env["TRACESTATE"] = carrier["tracestate"]
    return env


def attach_env_context() -> None:
    """Continue a trace started by a parent process (reads ``TRACEPARENT``)."""
    if not enabled() or not os.environ.get("TRACEPARENT"):
        return
    carrier = {"traceparent": os.environ["TRACEPARENT"]}
    if os.environ.get("TRACESTATE"):
        carrier["tracestate"] = os.environ["TRACESTATE"]
    otel_context.attach(TraceContextTextMapPropagator().extract(carrier))


# ----- exporter setup (for applications, not libraries) ----------------------------------------


def configure(service_name: str = "secsdk") -> str | None:
    """Set up an exporter from the environment. Returns the destination used, or None.

    Safe to call more than once. Libraries embedding secsdk should *not* call this;
    it is for applications (secctl, the agent example, the Azure Function).
    """
    global _configured
    if _configured or not enabled():
        return None
    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)

    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
        except ImportError:
            warnings.warn(
                "APPLICATIONINSIGHTS_CONNECTION_STRING is set but azure-monitor-opentelemetry "
                "is not installed (pip install 'secsdk[azure-monitor]')",
                stacklevel=2,
            )
            return None
        configure_azure_monitor()
        _configured = True
        atexit.register(_shutdown)
        return "azure-monitor"

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    )
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            warnings.warn(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but the OTLP exporter is not installed "
                "(pip install 'secsdk[otlp]')",
                stacklevel=2,
            )
            return None
        resource = Resource.create()
        tp = TracerProvider(resource=resource)
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        otel_trace.set_tracer_provider(tp)
        otel_metrics.set_meter_provider(
            MeterProvider(
                resource=resource,
                metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
            )
        )
        _configured = True
        atexit.register(_shutdown)
        return "otlp"

    path = os.environ.get("SECSDK_TELEMETRY_FILE")
    if path:
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
        except ImportError:
            warnings.warn(
                "SECSDK_TELEMETRY_FILE is set but opentelemetry-sdk is not installed "
                "(pip install 'secsdk[otlp]')",
                stacklevel=2,
            )
            return None
        stream = open(os.path.expanduser(path), "a", encoding="utf-8")  # noqa: SIM115 - closed at exit
        exporter = ConsoleSpanExporter(
            out=stream, formatter=lambda s: s.to_json(indent=None) + "\n"
        )
        tp = TracerProvider(resource=Resource.create())
        tp.add_span_processor(SimpleSpanProcessor(exporter))
        otel_trace.set_tracer_provider(tp)
        _configured = True
        atexit.register(_shutdown)
        atexit.register(stream.close)
        return "file"
    return None


def _shutdown() -> None:
    """Flush and stop providers so short-lived processes (the CLI) don't lose data."""
    for provider in (otel_trace.get_tracer_provider(), otel_metrics.get_meter_provider()):
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            with contextlib.suppress(Exception):  # telemetry must never fail the program
                shutdown()
