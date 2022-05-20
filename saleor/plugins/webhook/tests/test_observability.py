from unittest.mock import patch

from celery.canvas import Signature

from ....webhook.event_types import WebhookEventAsyncType
from ....webhook.observability import dump_payload
from ..tasks import (
    observability_reporter_task,
    observability_send_events,
    send_observability_events,
)


@patch("saleor.plugins.webhook.tasks.group")
@patch("saleor.plugins.webhook.tasks.send_observability_events")
@patch("saleor.plugins.webhook.tasks.observability.get_webhooks")
@patch("saleor.plugins.webhook.tasks.observability.buffer_pop_events")
def test_observability_reporter_task(
    mock_buffer_pop_events,
    mock_get_webhooks,
    mock_send_observability_events,
    mock_celery_group,
    observability_webhook_data,
    settings,
):
    events, batch_count = ["event", "event"], 5
    webhooks = [observability_webhook_data]
    mock_buffer_pop_events.return_value = events, batch_count
    mock_get_webhooks.return_value = webhooks

    observability_reporter_task()

    mock_celery_group.assert_called_once()
    tasks = mock_celery_group.call_args.args[0]
    for task in tasks:
        assert isinstance(task, Signature)
    assert len(tasks) == batch_count
    expires = settings.OBSERVABILITY_REPORT_PERIOD.total_seconds()
    mock_celery_group.return_value.apply_async.assert_called_once_with(expires=expires)
    mock_send_observability_events.assert_called_once_with(webhooks, events)


@patch("saleor.plugins.webhook.tasks.send_observability_events")
@patch("saleor.plugins.webhook.tasks.observability.get_webhooks")
@patch("saleor.plugins.webhook.tasks.observability.buffer_pop_events")
def test_observability_send_events(
    mock_buffer_pop_events,
    mock_get_webhooks,
    mock_send_observability_events,
    observability_webhook_data,
):
    events, batch_count = ["event", "event"], 5
    webhooks = [observability_webhook_data]
    mock_buffer_pop_events.return_value = events, batch_count
    mock_get_webhooks.return_value = webhooks

    observability_send_events()

    mock_send_observability_events.assert_called_once_with(webhooks, events)


@patch("saleor.plugins.webhook.tasks.send_webhook_using_scheme_method")
def test_send_observability_events(
    mock_send_webhook_using_scheme_method, observability_webhook_data
):
    webhooks = [observability_webhook_data]
    events = [{"event": "data"}, {"event": "data"}]

    send_observability_events(webhooks, events)

    mock_send_webhook_using_scheme_method.assert_called_once_with(
        observability_webhook_data.target_url,
        observability_webhook_data.saleor_domain,
        observability_webhook_data.secret_key,
        WebhookEventAsyncType.OBSERVABILITY,
        dump_payload(events),
    )


@patch("saleor.plugins.webhook.tasks.send_webhook_using_scheme_method")
def test_send_observability_events_to_google_pub_sub(
    mock_send_webhook_using_scheme_method, observability_webhook_data
):
    observability_webhook_data.target_url = (
        "gcpubsub://cloud.google.com/projects/saleor/topics/test"
    )
    webhooks = [observability_webhook_data]
    events = [{"event": "data"}, {"event": "data"}]

    send_observability_events(webhooks, events)

    assert mock_send_webhook_using_scheme_method.call_count == len(events)
    mock_send_webhook_using_scheme_method.assert_called_with(
        observability_webhook_data.target_url,
        observability_webhook_data.saleor_domain,
        observability_webhook_data.secret_key,
        WebhookEventAsyncType.OBSERVABILITY,
        dump_payload(events[-1]),
    )
