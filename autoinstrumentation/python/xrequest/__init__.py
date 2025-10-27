from __future__ import annotations

import hashlib
import os
import re
from logging import getLogger
from typing import Iterable, Optional, Sequence

from opentelemetry.instrumentation._semconv import _OpenTelemetrySemanticConventionStability, \
    _OpenTelemetryStabilitySignalType
from opentelemetry.propagate import set_global_textmap,get_global_textmap

from opentelemetry import propagators, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import Context
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import Getter, Setter, TextMapPropagator
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator as W3CTraceContextPropagator,
)

_logger = getLogger(__name__)

# ===================== 自定义 Propagator =====================

_DEFAULT_REQUEST_ID_HEADERS = (
    "requestid",
)
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _hex_to_int(h: str) -> int:
    return int(h, 16)


def _normalize_request_id_to_trace_id_hex(request_id: str) -> str:
    """
    将任意 request_id 规范化为 32 位十六进制（W3C trace id 要求）：
    - 若已是 32 位十六进制，直接返回；
    - 若为 UUID，去掉 '-' 后返回；
    - 否则 sha256 后取前 32 位。
    """
    if request_id is None:
        request_id = "00000000000000000000000000000000"
    rid = request_id.strip().lower()
    if _HEX32_RE.match(rid):
        return rid
    if _UUID_RE.match(rid):
        return rid.replace("-", "")
    return hashlib.sha256(rid.encode("utf-8")).hexdigest()[:32]


def _extract_request_id_from_headers(
    carrier, getter: Getter, header_candidates: Sequence[str]
) -> Optional[str]:
    for k in header_candidates:
        values = getter.get(carrier, k)
        if values:
            return values[0]
    return None


class RequestIdFallbackTracePropagator(TextMapPropagator):
    """
    规则：
    1) 先用标准 W3C 提取（traceparent/tracestate）。
    2) 如无效且存在 request id，则构造远程父 SpanContext：
       - trace_id = 由 request id 规范化而来
       - span_id = 固定 parent_span_id_hex（例如 bbbbbbbbbbbbbbbb）
       - is_remote = True
       - trace_flags = SAMPLED（可按需调整）
    3) 注入仍使用标准 W3C（保持兼容）。
    """

    def __init__(
        self,
        request_id_headers: Sequence[str] = _DEFAULT_REQUEST_ID_HEADERS,
        parent_span_id_hex: str = "bbbbbbbbbbbbbbbb",
        delegate: TextMapPropagator | None = None,
        sampled: bool = True,
    ):
        if not re.fullmatch(r"[0-9a-fA-F]{16}", parent_span_id_hex):
            raise ValueError("parent_span_id_hex 必须是 16 位十六进制字符串")
        self._request_id_headers = tuple(h.lower() for h in request_id_headers)
        self._parent_span_id_hex = parent_span_id_hex.lower()
        self._parent_span_id_int = _hex_to_int(self._parent_span_id_hex)
        self._delegate = delegate or W3CTraceContextPropagator()
        self._trace_flags = TraceFlags(TraceFlags.SAMPLED if sampled else 0)

    def extract(self, carrier, context: Context = Context(), getter: Getter = None) -> Context:
        getter = getter or propagators.default_getter

        # 1) 首选标准 W3C 提取
        ctx = self._delegate.extract(carrier, context=context, getter=getter)
        current = trace.get_current_span(ctx)
        if current and current.get_span_context().is_valid:
            return ctx
        print(self._request_id_headers)
        # 2) 无有效 traceparent，回退到 request id
        req_id = _extract_request_id_from_headers(carrier, getter, self._request_id_headers)
        if not req_id:
            return ctx  # 保持无 parent 的上下文

        trace_id_hex = _normalize_request_id_to_trace_id_hex(req_id)
        parent_sc = SpanContext(
            trace_id=_hex_to_int(trace_id_hex),
            span_id=self._parent_span_id_int,
            is_remote=True,
            trace_flags=self._trace_flags,
            trace_state=trace.DEFAULT_TRACE_STATE,
        )
        parent_span = NonRecordingSpan(parent_sc)
        return trace.set_span_in_context(parent_span, ctx)

    def inject(self, carrier, context: Context = Context(), setter: Setter = None) -> None:
        self._delegate.inject(carrier, context=context, setter=setter)

    def fields(self) -> Iterable[str]:
        return self._delegate.fields()


# ===================== 仅负责设置全局 Propagator 的 Instrumentation =====================

class RequestIdPropagatorInstrumentor(BaseInstrumentor):
    """
    这个 Instrumentation 不注入任何中间件或框架逻辑，唯一职责是：
    - instrument() 时，设置全局 TextMapPropagator 为：
        CompositePropagator([RequestIdFallbackTracePropagator(...), W3CBaggagePropagator()])
    - uninstrument() 时，恢复之前的全局 propagator。
    """

    def __init__(
        self,
        request_id_headers: Sequence[str] = _DEFAULT_REQUEST_ID_HEADERS,
        parent_span_id_hex: str = "bbbbbbbbbbbbbbbb",
        sampled: bool = True,
    ):
        super().__init__()
        self._request_id_headers = tuple(h.lower() for h in request_id_headers)
        self._parent_span_id_hex = parent_span_id_hex
        self._sampled = sampled
        self._previous = None  # 保存之前的全局 propagator

    def instrumentation_dependencies(self) -> list[str]:
        # 无额外依赖声明
        return []

    def _instrument(self, **kwargs):
        # 允许运行时覆盖（通过 instrument(...) 传参）
        request_id_headers = tuple(
            (kwargs.get("request_id_headers") or self._request_id_headers)
        )
        parent_span_id_hex = kwargs.get("parent_span_id_hex", self._parent_span_id_hex)
        sampled = kwargs.get("sampled", self._sampled)

        # 记录原全局 propagator
        self._previous = get_global_textmap()

        # 设定新的全局 propagator
        custom_trace = RequestIdFallbackTracePropagator(
            request_id_headers=request_id_headers,
            parent_span_id_hex=parent_span_id_hex,
            sampled=sampled,
        )

        set_global_textmap(
            CompositePropagator([custom_trace, W3CBaggagePropagator()])
        )

    def _uninstrument(self, **kwargs):
        if self._previous is not None:
            set_global_textmap(self._previous)
            self._previous = None

    def instrument_app(self):
        self._instrument()
