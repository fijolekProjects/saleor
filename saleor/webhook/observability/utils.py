import functools
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Dict, Generator, List, Optional, Tuple

import opentracing
import opentracing.tags
from asgiref.local import Local
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.utils import timezone
from graphql import GraphQLDocument
from pytimeparse import parse

from ..event_types import WebhookEventAsyncType
from ..utils import get_webhooks_for_event
from .buffers import get_buffer
from .payloads import generate_api_call_payload, generate_event_delivery_attempt_payload

if TYPE_CHECKING:

    from celery.exceptions import Retry
    from django.http import HttpRequest, HttpResponse

    from ...core.models import EventDeliveryAttempt

logger = logging.getLogger(__name__)
CACHE_TIMEOUT = parse("2 minutes")
BUFFER_KEY = "observability_buffer"
WEBHOOKS_KEY = "observability_webhooks"
_active_webhooks_exists_cache: Dict[str, Tuple[bool, float]] = {}
_context = Local()


@dataclass
class WebhookData:
    id: int
    saleor_domain: str
    target_url: str
    secret_key: Optional[str] = None


def get_buffer_name() -> str:
    return cache.make_key(BUFFER_KEY)


def get_observability_webhooks() -> List[WebhookData]:
    with opentracing.global_tracer().start_active_span(
        "get_observability_webhooks"
    ) as scope:
        scope.span.set_tag(opentracing.tags.COMPONENT, "observability")
        webhooks_data = cache.get(WEBHOOKS_KEY)
        if webhooks_data is None:
            webhooks_data = []
            event_type = WebhookEventAsyncType.OBSERVABILITY
            if webhooks := get_webhooks_for_event(event_type):
                domain = Site.objects.get_current().domain
                for webhook in webhooks:
                    webhooks_data.append(
                        WebhookData(
                            id=webhook.id,
                            saleor_domain=domain,
                            target_url=webhook.target_url,
                            secret_key=webhook.secret_key,
                        )
                    )
            cache.set(WEBHOOKS_KEY, webhooks_data, timeout=CACHE_TIMEOUT)
        return webhooks_data


def active_webhooks_exists_clear_cache():
    _active_webhooks_exists_cache.clear()


def active_webhooks_exists(timeout=CACHE_TIMEOUT) -> bool:
    key = get_buffer_name()
    cached = _active_webhooks_exists_cache.get(key, None)
    if cached is not None:
        is_active, check_time = cached
        if monotonic() - check_time <= timeout:
            return is_active
    is_active = bool(get_observability_webhooks())
    _active_webhooks_exists_cache[key] = (is_active, monotonic())
    return is_active


def task_next_retry_date(retry_error: "Retry") -> Optional[datetime]:
    if isinstance(retry_error.when, (int, float)):
        return timezone.now() + timedelta(seconds=retry_error.when)
    if isinstance(retry_error.when, datetime):
        return retry_error.when
    return None


def put_to_buffer(generate_payload: Callable[[], Any]):
    try:
        payload = generate_payload()
        get_buffer(get_buffer_name()).put_event(payload)
    except Exception:
        logger.error("[Observability] Event dropped", exc_info=True)


@dataclass
class GraphQLOperationResponse:
    name: Optional[str] = None
    query: Optional[GraphQLDocument] = None
    variables: Optional[Dict] = None
    result: Optional[Dict] = None
    result_invalid: bool = False


class ApiCall:
    def __init__(self, request: "HttpRequest"):
        self.gql_operations: List[GraphQLOperationResponse] = []
        self.response: Optional["HttpResponse"] = None
        self._reported = False
        self.request = request

    def report(self):
        if self._reported or not settings.OBSERVABILITY_ACTIVE:
            return
        only_app_api_call = not settings.OBSERVABILITY_REPORT_ALL_API_CALLS
        if only_app_api_call and getattr(self.request, "app", None) is None:
            return
        if self.response is None:
            logger.error("[Observability] HttpResponse not provided, event dropped")
            return
        self._reported = True
        with opentracing.global_tracer().start_active_span(
            "observability_report_api_call"
        ) as scope:
            scope.span.set_tag(opentracing.tags.COMPONENT, "observability")
            if active_webhooks_exists():
                put_to_buffer(
                    partial(
                        generate_api_call_payload,
                        self.request,
                        self.response,
                        self.gql_operations,
                        settings.OBSERVABILITY_MAX_PAYLOAD_SIZE,
                    )
                )


@contextmanager
def report_api_call(request: "HttpRequest") -> Generator[ApiCall, None, None]:
    root = False
    if not hasattr(_context, "api_call"):
        _context.api_call, root = ApiCall(request), True
    yield _context.api_call
    if root:
        _context.api_call.report()
        del _context.api_call


@contextmanager
def report_gql_operation() -> Generator[GraphQLOperationResponse, None, None]:
    root = False
    if not hasattr(_context, "gql_operation"):
        _context.gql_operation, root = GraphQLOperationResponse(), True
    yield _context.gql_operation
    if root:
        if hasattr(_context, "api_call"):
            _context.api_call.gql_operations.append(_context.gql_operation)
        del _context.gql_operation


def report_view(method):
    @functools.wraps(method)
    def wrapper(self, request, *args, **kwargs):
        with report_api_call(request) as api_call:
            response = method(self, request, *args, **kwargs)
            api_call.response = response
            return response

    return wrapper


def report_event_delivery_attempt(
    attempt: "EventDeliveryAttempt", next_retry: Optional["datetime"] = None
):
    if not settings.OBSERVABILITY_ACTIVE:
        return
    if attempt.delivery is None:
        logger.error(
            "[Observability] %r not assigned to delivery. Event dropped", attempt
        )
    with opentracing.global_tracer().start_active_span(
        "observability_report_event_delivery_attempt"
    ) as scope:
        scope.span.set_tag(opentracing.tags.COMPONENT, "observability")
        if active_webhooks_exists():
            put_to_buffer(
                partial(
                    generate_event_delivery_attempt_payload,
                    attempt,
                    next_retry,
                    settings.OBSERVABILITY_MAX_PAYLOAD_SIZE,
                )
            )
