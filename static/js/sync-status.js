/* One exact refresh run, shared by the two dashboard adapters. */
(function() {
    'use strict';

    window.createRiveterSyncMonitor = function(onChange) {
        var watching = false;
        var timer = null;
        var pending = null;
        var posting = false;
        var generation = 0;
        var runId = null;
        var expiredRunId = null;
        var expires = 0;
        var phase = 'idle';
        var previous = '';
        var lastStatus = null;

        function emit(state, message, status, identity) {
            phase = state;
            if (status) lastStatus = status;
            var id = identity || runId;
            var key = [state, id || '', message || ''].join('|');
            var changed = key !== previous;
            previous = key;
            onChange({
                state: state, run_id: id, message: message || '',
                busy: state === 'running' || state === 'submitting',
                changed: changed, status: lastStatus
            });
        }

        function settle(state, message, status) {
            var identity = runId;
            runId = null;
            emit(state, message, status, identity);
        }

        function adopt(active) {
            if (!active || typeof active.run_id !== 'string' || !active.run_id
                    || active.run_id === expiredRunId) return;
            runId = active.run_id;
            expires = Date.now() + 10 * 60 * 1000;
        }

        function expire(status) {
            if (!runId || Date.now() < expires) return false;
            expiredRunId = runId;
            settle('unconfirmed', 'Timed out confirming refresh status. The server has not been cancelled.', status);
            return true;
        }

        function markerMatches(status, identity) {
            var marker = status.last_sync;
            if (!marker || !Number.isInteger(marker.id) || marker.id <= 0
                    || marker.sync_type !== 'full_scan') return false;
            try {
                return JSON.parse(marker.result_summary).run_id === identity;
            } catch (_) {
                return false;
            }
        }

        function accept(status, runner) {
            var active = (runner._runs || {}).sync;
            var done = (runner._completed || {}).sync;
            if (!runId) adopt(active);
            if (!runId) {
                if (status.sync_running || (active && active.run_id)) {
                    emit('unconfirmed', 'Refresh is running, but its status could not be confirmed.', status);
                } else {
                    emit('idle', '', status);
                }
                return;
            }
            if (active && active.run_id === runId) {
                emit('running', 'Refreshing Microsoft 365...', status);
            } else if (done && done.run_id === runId) {
                if (done.error || (typeof done.exit_code === 'number' && done.exit_code !== 0)
                        || ['failed', 'blocked', 'partial'].indexOf(done.state) !== -1) {
                    settle('failed', done.error || 'The refresh did not finish.', status);
                } else if (done.state === 'succeeded' && done.exit_code === 0
                        && markerMatches(status, runId)) {
                    settle('succeeded', 'Sync complete', status);
                } else {
                    emit('unconfirmed', 'Confirming refresh completion...', status);
                }
            } else {
                emit('unconfirmed', 'Could not confirm refresh status. It may still be running.', status);
            }
            expire(status);
            if (!runId && active && active.run_id) {
                adopt(active);
                if (runId) emit('running', 'Refreshing Microsoft 365...', status);
            }
        }

        async function read(url, options) {
            var controller = new AbortController();
            var timeout = setTimeout(function() { controller.abort(); }, 15000);
            try {
                var response = await fetch(url, Object.assign({}, options, {signal: controller.signal}));
                var data = await response.json();
                if (!response.ok) throw new Error(data.message || data.error || 'Could not read refresh status.');
                return data;
            } finally {
                clearTimeout(timeout);
            }
        }

        function schedule() {
            clearTimeout(timer);
            timer = null;
            if (watching && !posting) timer = setTimeout(poll, runId ? 5000 : 30000);
        }

        function poll() {
            if (pending) return pending;
            if (posting) return Promise.resolve();
            clearTimeout(timer);
            timer = null;
            var token = generation;
            pending = Promise.all([read('/api/sync-status'), read('/api/runner-status')])
                .then(function(values) {
                    if (token === generation) accept(values[0], values[1]);
                })
                .catch(function() {
                    if (token === generation) {
                        if (!expire()) {
                            emit('unconfirmed', 'Could not confirm refresh status. It may still be running.');
                        }
                    }
                })
                .finally(function() {
                    pending = null;
                    schedule();
                });
            return pending;
        }

        async function sync() {
            if (posting || phase === 'running') return;
            var token = ++generation;
            var olderPoll = pending;
            clearTimeout(timer);
            posting = true;
            runId = null;
            emit('submitting', 'Starting refresh...');
            try {
                var data = await read('/api/sync-status', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: '{}'
                });
                if (token !== generation) return;
                if (!data.ok) throw new Error(data.message || 'Could not start refresh.');
                if (typeof data.run_id !== 'string' || !data.run_id) {
                    emit('unconfirmed', 'The refresh started, but its run could not be identified.');
                } else {
                    adopt(data);
                    emit('running', 'Refreshing Microsoft 365...');
                }
            } catch (error) {
                if (token === generation) {
                    emit('unconfirmed', error.message || 'Could not confirm whether refresh started.');
                }
            } finally {
                if (token === generation) posting = false;
            }
            if (olderPoll) await olderPoll;
            if (token === generation) return poll();
        }

        function start() {
            if (watching) return;
            watching = true;
            return poll();
        }

        function stop() {
            watching = false;
            generation += 1;
            posting = false;
            clearTimeout(timer);
            timer = null;
        }

        window.addEventListener('pagehide', stop);
        window.addEventListener('pageshow', start);
        return {start: start, poll: poll, sync: sync, stop: stop};
    };
})();
