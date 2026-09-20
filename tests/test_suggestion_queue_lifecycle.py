"""Application ownership contracts for the suggestion-check queue pump."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import app as app_module


def test_make_app_constructs_passive_queue_without_periodic_callback():
    with patch.object(app_module.tornado.ioloop, "PeriodicCallback") as periodic:
        application = app_module.make_app()

    assert application.suggestion_check_queue is not None
    assert application.suggestion_check_queue.post_sync_initialized is False
    assert application.suggestion_check_queue_callback is None
    periodic.assert_not_called()


def test_periodic_global_yields_to_targeted_queue():
    queue = Mock()
    queue.has_work.return_value = True

    with (
        patch.object(app_module, "get_connection") as get_connection,
        patch("src.services.claude_runner.run_copilot") as run_copilot,
    ):
        app_module._check_suggestions(queue)

    get_connection.assert_not_called()
    run_copilot.assert_not_called()


def test_start_server_registers_and_starts_one_queue_pump():
    queue = Mock()
    application = SimpleNamespace(
        suggestion_check_queue=queue,
        suggestion_check_queue_callback=None,
        listen=Mock(),
    )
    callbacks = []
    initialization_order = []

    def periodic_callback(callback, interval):
        initialization_order.append(("callback", interval))
        item = Mock()
        item.callback = callback
        item.interval = interval
        item.start.side_effect = lambda: initialization_order.append(("start", interval))
        callbacks.append(item)
        return item

    connection = Mock()
    queue.initialize_post_sync.side_effect = (
        lambda enabled_reader: initialization_order.append(("initialize", enabled_reader))
    )
    application.listen.side_effect = lambda *args, **kwargs: initialization_order.append(("listen", None))
    with (
        patch.object(app_module, "get_connection", return_value=connection),
        patch.object(app_module, "init_db", side_effect=lambda conn: initialization_order.append(("init_db", conn))) as init_db,
        patch.object(app_module, "_recover_stuck_parses", side_effect=lambda: initialization_order.append(("recover", None))) as recover,
        patch.object(app_module, "recover_stuck_previews", return_value=0),
        patch.object(app_module, "missing_settings_warning", return_value=None),
        patch.object(app_module, "make_app", return_value=application),
        patch.object(app_module, "get_workspace_settings", return_value={}),
        patch.object(app_module.threading, "Thread") as thread,
        patch.object(
            app_module.tornado.ioloop,
            "PeriodicCallback",
            side_effect=periodic_callback,
        ),
        patch.object(app_module, "_backup_db"),
        patch.object(
            app_module.tornado.ioloop.IOLoop,
            "current",
            return_value=Mock(),
        ),
    ):
        returned, _ = app_module.start_server(0)

    assert returned is application
    init_db.assert_called_once_with(connection)
    recover.assert_called_once_with()
    queue.initialize_post_sync.assert_called_once()
    assert queue.initialize_post_sync.call_args.args[0]() is True
    names = [event[0] for event in initialization_order]
    assert names[:4] == ["init_db", "recover", "initialize", "listen"]
    assert initialization_order.index(("recover", None)) < initialization_order.index(
        ("start", app_module.PARSE_CHECK_INTERVAL_MS)
    )
    queue_callbacks = [
        item for item in callbacks
        if item.callback == queue.pump_once
        and item.interval == app_module.SUGGESTION_QUEUE_PUMP_INTERVAL_MS
    ]
    assert len(queue_callbacks) == 1
    queue_callbacks[0].start.assert_called_once_with()
    assert application.suggestion_check_queue_callback is queue_callbacks[0]
    application.listen.assert_called_once_with(0, address="127.0.0.1")
    assert thread.call_count == 2


def test_queue_callback_stops_during_server_shutdown():
    callback = Mock()
    app_module._stop_suggestion_queue(SimpleNamespace(
        suggestion_check_queue_callback=callback
    ))
    callback.stop.assert_called_once_with()


def test_periodic_global_launches_direct_batch_without_cli():
    with (
        patch("src.services.claude_runner.run_copilot", side_effect=AssertionError("CLI forbidden")),
        patch.object(app_module, "checks") as checks,
    ):
        app_module._check_suggestions()
    checks.get_checks.return_value.launch.assert_called_once_with(skip_empty=True)


def test_start_server_registers_three_hour_suggestion_callback():
    assert app_module.SUGGESTION_CHECK_INTERVAL_MS == 3 * 60 * 60 * 1000
