/**
 * briefing-api.js — Adapter that replaces mock briefing content with live data.
 *
 * Injected into mock-briefing.html by BriefingPageHandler.
 * Fetches /api/briefing, replaces each section if data is available.
 * If stale or missing, shows a banner and polls until refresh completes.
 */
(function () {
  'use strict';

  var POLL_INTERVAL = 5000; // 5 seconds while refresh is running
  var pollTimer = null;

  // ── Utility ──────────────────────────────────────────────
  function esc(s) {
    if (!s) return '';
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function priorityDot(p) {
    return '<span class="priority-dot p' + (p || 3) + '"></span>';
  }

  // ── Banner ───────────────────────────────────────────────
  function showBanner(msg, type) {
    var existing = document.getElementById('briefing-banner');
    if (existing) existing.remove();

    var banner = document.createElement('div');
    banner.id = 'briefing-banner';
    banner.style.cssText =
      'padding:10px 20px;font-size:13px;font-weight:500;display:flex;' +
      'align-items:center;gap:8px;border-bottom:1px solid var(--border);';

    if (type === 'refreshing') {
      banner.style.background = 'var(--ai-light)';
      banner.style.color = 'var(--ai)';
      banner.innerHTML = '<span class="spin">✦</span> ' + esc(msg);
    } else if (type === 'error') {
      banner.style.background = '#fce4ec';
      banner.style.color = '#c62828';
      banner.innerHTML = '⚠ ' + esc(msg);
    } else if (type === 'stale') {
      banner.style.background = '#fff3e0';
      banner.style.color = '#e65100';
      banner.innerHTML = '⏳ ' + esc(msg) +
        ' <button onclick="triggerBriefingRefresh()" style="margin-left:auto;padding:4px 12px;' +
        'border-radius:4px;border:1px solid currentColor;background:transparent;color:inherit;' +
        'cursor:pointer;font-family:inherit;font-size:12px;font-weight:600">Refresh now</button>';
    }

    var contentBody = document.querySelector('.content-body');
    if (contentBody) contentBody.parentNode.insertBefore(banner, contentBody);
  }

  function removeBanner() {
    var b = document.getElementById('briefing-banner');
    if (b) b.remove();
  }

  // ── CSS for spinner ──────────────────────────────────────
  var style = document.createElement('style');
  style.textContent = '@keyframes briefing-spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}' +
    '.spin{display:inline-block;animation:briefing-spin 1.5s linear infinite}';
  document.head.appendChild(style);

  // ── Person tasks panel ─────────────────────────────────
  window._showPersonTasks = function (personName) {
    var panel = document.getElementById('briefing-detail');
    var overlay = document.getElementById('bd-overlay');
    panel.innerHTML = '<div class="bd-loading">Loading tasks for ' + personName + '...</div>';
    panel.classList.add('open');
    overlay.classList.add('open');

    fetch('/api/tasks?limit=2000')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var tasks = (data.tasks || data || []).filter(function (t) {
          if (!t.key_people) return false;
          try {
            var kp = typeof t.key_people === 'string' ? JSON.parse(t.key_people) : t.key_people;
            return kp.some(function (p) {
              return (p.name || '').toLowerCase().indexOf(personName.toLowerCase()) >= 0;
            });
          } catch (e) { return false; }
        });

        if (!tasks.length) {
          panel.innerHTML = '<div class="bd-header"><div class="bd-title">' + esc(personName) + '</div>' +
            '<button class="bd-close" onclick="closeTaskPanel()">✕</button></div>' +
            '<div class="bd-loading">No matching tasks found</div>';
          return;
        }

        var statusOrder = { in_progress: 0, active: 1, waiting: 2, suggested: 3 };
        tasks.sort(function (a, b) {
          return (statusOrder[a.status] || 9) - (statusOrder[b.status] || 9) || a.priority - b.priority;
        });

        var statusColors = {
          active: 'var(--accent)', in_progress: 'var(--success)', waiting: '#e65100',
          suggested: 'var(--text-muted)', completed: 'var(--success)', dismissed: 'var(--text-muted)'
        };

        var html = '<div class="bd-header"><div class="bd-title">👤 ' + esc(personName) + '</div>' +
          '<button class="bd-close" onclick="closeTaskPanel()">✕</button></div>' +
          '<div class="bd-id">' + tasks.length + ' tasks</div><hr class="bd-sep">';

        html += tasks.map(function (t) {
          return '<div style="padding:8px 20px;cursor:pointer;border-bottom:1px solid var(--border)" ' +
            'onclick="openTaskPanel(' + t.id + ')" onmouseover="this.style.background=\'var(--bg-hover)\'" ' +
            'onmouseout="this.style.background=\'\'">' +
            '<div style="font-size:13px;font-weight:500">#' + t.id + ' · ' + esc(t.title) + '</div>' +
            '<div style="font-size:11px;color:var(--text-muted);margin-top:2px">' +
            '<span style="color:' + (statusColors[t.status] || 'inherit') + '">' + esc(t.status) + '</span>' +
            ' · P' + t.priority + '</div></div>';
        }).join('');

        panel.innerHTML = html;
      })
      .catch(function () {
        panel.innerHTML = '<div class="bd-loading">Could not load tasks</div>';
      });
  };

  // ── Button action handlers ─────────────────────────────
  function handleAction(a) {
    if (!a) return;
    // Promote/dismiss/start/complete → task action API
    if (a.action && a.task_id) {
      fetch('/api/tasks/' + a.task_id + '/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: a.action })
      }).then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.task) {
            showToast((a.action === 'promote' ? 'Promoted' : a.action === 'start' ? 'Started' : 'Updated') + ' #' + a.task_id);
          }
        });
      return;
    }
    // Skill generation (AI buttons)
    if (a.type === 'ai' && a.task_id) {
      fetch('/api/tasks/' + a.task_id + '/skill', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok) showToast('Generating skill output for #' + a.task_id + '...');
          else showToast(d.message || 'Could not start skill', true);
        });
      openTaskPanel(a.task_id);
      return;
    }
    // Open task in panel
    if (a.task_id) {
      openTaskPanel(a.task_id);
      return;
    }
    // Navigate to TodoIQ
    if (a.href) {
      window.location.href = a.href;
      return;
    }
  }

  function showToast(msg, isError) {
    var toast = document.createElement('div');
    toast.style.cssText =
      'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);padding:10px 20px;' +
      'border-radius:8px;font-size:13px;font-weight:500;z-index:200;' +
      'box-shadow:0 4px 12px rgba(0,0,0,0.15);transition:opacity 0.3s;' +
      (isError ? 'background:#d13438;color:#fff' : 'background:var(--text);color:var(--bg)');
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(function () { toast.style.opacity = '0'; }, 2500);
    setTimeout(function () { toast.remove(); }, 3000);
  }

  function actionButton(a, btnClass) {
    var cls = btnClass || (a.type === 'ai' ? 'ai' : a.type === 'primary' ? 'primary' : 'secondary');
    var prefix = a.type === 'ai' ? '✦ ' : '';
    var dataAttr = '';
    if (a.task_id) dataAttr += ' data-task-id="' + a.task_id + '"';
    if (a.action) dataAttr += ' data-action="' + esc(a.action) + '"';
    if (a.href) dataAttr += ' data-href="' + esc(a.href) + '"';
    return '<button class="init-btn ' + cls + '"' + dataAttr + ' onclick="window._briefingAction(this)">' +
      prefix + esc(a.label) + '</button>';
  }

  function insightActionButton(a) {
    var cls = a.type === 'ai' ? 'ai' : a.type === 'primary' ? 'primary' : 'secondary';
    var prefix = a.type === 'ai' ? '✦ ' : '';
    var dataAttr = '';
    if (a.task_id) dataAttr += ' data-task-id="' + a.task_id + '"';
    if (a.action) dataAttr += ' data-action="' + esc(a.action) + '"';
    if (a.href) dataAttr += ' data-href="' + esc(a.href) + '"';
    return '<button class="insight-btn ' + cls + '"' + dataAttr + ' onclick="window._briefingAction(this)">' +
      prefix + esc(a.label) + '</button>';
  }

  // Global click handler for action buttons
  window._briefingAction = function (el) {
    var a = {};
    if (el.dataset.taskId) a.task_id = parseInt(el.dataset.taskId);
    if (el.dataset.action) a.action = el.dataset.action;
    if (el.dataset.href) a.href = el.dataset.href;
    if (el.classList.contains('ai')) a.type = 'ai';
    else if (el.classList.contains('primary')) a.type = 'primary';
    else a.type = 'secondary';
    handleAction(a);
  };

  // ── Section renderers ────────────────────────────────────

  function renderAttention(data) {
    if (!data || !data.attention) return;
    var att = data.attention;

    // Stale follow-ups
    if (att.stale_followups && att.stale_followups.length > 0) {
      var card = document.querySelector('.insight-card.risk');
      if (card) {
        var items = att.stale_followups;
        var topPeople = items.slice(0, 3).map(function (i) { return '<strong>' + esc(i.person) + '</strong>'; });
        card.querySelector('.insight-title').innerHTML =
          items.length + ' people have been waiting on you for over a week';
        card.querySelector('.insight-body').innerHTML =
          'Your longest-open waiting items are with ' + topPeople.join(', ') +
          '. A quick "still on my radar" message prevents relationship damage.';

        var tasksEl = card.querySelector('.insight-tasks');
        if (tasksEl) {
          tasksEl.innerHTML = items.slice(0, 5).map(function (i) {
            return '<div class="insight-task">' + priorityDot(i.priority) +
              '<span class="it-id">#' + i.task_id + '</span>' +
              '<span class="it-title">' + esc(i.title) + '</span>' +
              '<span class="it-meta">' + i.days_waiting + 'd waiting</span></div>';
          }).join('');
        }

        // Wire up action buttons
        var actionsEl = card.querySelector('.insight-actions');
        if (actionsEl) {
          actionsEl.innerHTML =
            insightActionButton({ label: 'Draft quick check-ins', type: 'ai', task_id: items[0].task_id }) +
            insightActionButton({ label: 'View all ' + items.length + ' waiting →', type: 'secondary', href: '/todo' });
        }
      }
    }
  }

  function renderInitiatives(data) {
    if (!data || !data.initiatives || !data.initiatives.length) return;

    var grid = document.querySelector('.initiative-grid');
    if (!grid) return;

    grid.innerHTML = data.initiatives.map(function (init) {
      var healthClass = (init.health || 'on-track').replace(/[\s_]/g, '-').toLowerCase();
      var healthLabel = init.health || 'On Track';
      // Capitalize first letter of each word
      healthLabel = healthLabel.replace(/\b\w/g, function (c) { return c.toUpperCase(); });

      var meta = '<span class="meta-item">📋 ' + (init.task_count || 0) + ' tasks</span>' +
        '<span class="meta-item">⏳ ' + (init.waiting_count || 0) + ' waiting</span>' +
        '<span class="meta-item">👥 ' + (init.people ? init.people.length : 0) + ' people</span>';

      var actions = '';
      if (init.actions && init.actions.length) {
        actions = '<div class="init-actions">' + init.actions.map(function (a) {
          return actionButton(a);
        }).join('') + '</div>';
      }

      return '<div class="init-col">' +
        '<div class="init-col-header">' +
        '<div class="init-col-name">' + esc(init.name) + '</div>' +
        '<span class="init-health ' + healthClass + '">' + esc(healthLabel) + '</span>' +
        '<div class="init-col-meta">' + meta + '</div>' +
        '</div>' +
        '<div class="init-cos">' +
        '<div class="init-cos-label">✦ Status Update</div>' +
        '<div class="init-cos-text" style="font-style:normal">' + (init.cos_narrative || '') + '</div>' +
        actions +
        '</div></div>';
    }).join('');
  }

  function renderCalendar(data) {
    if (!data || !data.calendar) return;
    var cal = data.calendar;

    // Today's calendar card
    var prepCard = document.querySelector('.insight-card.prep');
    if (prepCard && cal.today_summary) {
      prepCard.querySelector('.insight-title').innerHTML = esc(cal.today_summary);

      if (cal.today_meetings && cal.today_meetings.length) {
        var body = cal.today_meetings.map(function (m) {
          var related = m.related_task_ids && m.related_task_ids.length
            ? ' — related: ' + m.related_task_ids.map(function (id) { return '#' + id; }).join(', ')
            : '';
          return '<strong>' + esc(m.time) + '</strong> ' + esc(m.title) +
            (m.has_agenda === false ? ' <em>(no agenda)</em>' : '') + related;
        }).join('<br>');
        prepCard.querySelector('.insight-body').innerHTML = body;
      }

      var tasksEl = prepCard.querySelector('.insight-tasks');
      if (tasksEl) tasksEl.innerHTML = '';

      // Replace single card body with per-meeting preps if available
      if (cal.meeting_preps && cal.meeting_preps.length) {
        var bodyEl = prepCard.querySelector('.insight-body');
        if (bodyEl) bodyEl.innerHTML = '';
        if (tasksEl) tasksEl.innerHTML = '';

        // Insert meeting prep cards after the main prep card
        var container = prepCard.parentNode;
        // Remove any previously injected meeting cards
        container.querySelectorAll('.meeting-prep-card').forEach(function (el) { el.remove(); });

        cal.meeting_preps.forEach(function (mp) {
          var card = document.createElement('div');
          card.className = 'insight-card prep meeting-prep-card';
          card.style.marginTop = '8px';

          var taskRows = '';
          if (mp.open_tasks && mp.open_tasks.length) {
            taskRows = '<div class="insight-tasks" style="margin-top:6px">' +
              mp.open_tasks.map(function (t) {
                return '<div class="insight-task">' +
                  '<span class="it-id">#' + t.id + '</span>' +
                  '<span class="it-title">' + esc(t.title) + '</span>' +
                  '<span class="it-meta">' + esc(t.status) + '</span></div>';
              }).join('') + '</div>';
          }

          var actions = '';
          if (mp.actions && mp.actions.length) {
            actions = '<div class="insight-actions">' + mp.actions.map(function (a) {
              return insightActionButton(a);
            }).join('') + '</div>';
          }

          card.innerHTML =
            '<div class="insight-icon">📅</div>' +
            '<div class="insight-label">' + esc(mp.time) + '</div>' +
            '<div class="insight-title">' + esc(mp.title) + '</div>' +
            '<div class="insight-body" style="font-size:12px;color:var(--text-muted);margin-bottom:4px">' +
            'With: ' + (mp.attendees || []).map(function (a) { return esc(a); }).join(', ') + '</div>' +
            '<div class="insight-body">' + esc(mp.prep_insight || '') + '</div>' +
            taskRows + actions;

          container.insertBefore(card, prepCard.nextSibling);
        });

        // Update summary card to just show the count
        prepCard.querySelector('.insight-title').innerHTML = esc(cal.today_summary);
        if (bodyEl) bodyEl.innerHTML = cal.meeting_preps.length + ' meetings with prep notes below';
      }
    }

    // Week load
    var weekCard = document.querySelector('.insight-card.info');
    if (weekCard && cal.week_load && cal.week_load.length) {
      var maxHours = Math.max.apply(null, cal.week_load.map(function (d) { return d.hours || 0; }));
      if (maxHours < 1) maxHours = 8;

      var barsHtml = cal.week_load.map(function (d) {
        var pct = Math.round(((d.hours || 0) / 8) * 100);
        var level = d.hours >= 5 ? 'heavy' : d.hours >= 3 ? 'medium' : 'light';
        var todayClass = d.is_today ? ' today' : '';
        return '<div class="day-load-bar' + todayClass + '">' +
          '<div class="bar-label">' + esc(d.day) + '</div>' +
          '<div class="bar-fill-container"><div class="bar-fill ' + level + '" style="width:' + pct + '%"></div></div>' +
          '<div class="bar-hours">' + d.hours + 'h</div></div>';
      }).join('');

      var loadEl = weekCard.querySelector('.day-load');
      if (loadEl) loadEl.innerHTML = barsHtml;

      if (cal.recommendation) {
        var recEl = weekCard.querySelector('.rec-text');
        if (recEl) recEl.innerHTML = '<strong>Recommendation:</strong> ' + esc(cal.recommendation);
      }
    }
  }

  function renderPeople(data) {
    if (!data || !data.people || !data.people.length) return;

    var strip = document.querySelector('.people-strip');
    if (!strip) return;

    strip.innerHTML = data.people.map(function (p) {
      var badge = '';
      if (p.badge) {
        var badgeType = p.badge_type || 'risk';
        badge = '<span class="chip-badge ' + badgeType + '">' + esc(p.badge) + '</span>';
      }
      return '<div class="person-chip" style="cursor:pointer" onclick="window._showPersonTasks(\'' +
        esc(p.name).replace(/'/g, "\\'") + '\')">' +
        '<div class="person-avatar" style="background:' + (p.color || '#616161') + '">' +
        esc(p.initials || '') + '</div>' +
        '<div><div class="person-chip-name">' + esc(p.name) + '</div>' +
        '<div class="person-chip-detail">' + esc(p.detail || '') + '</div></div>' +
        badge + '</div>';
    }).join('');

    // Relationship insight
    if (data.relationship_insight) {
      var ri = data.relationship_insight;
      var insightCard = document.querySelector('.insight-card.opportunity');
      if (insightCard) {
        insightCard.querySelector('.insight-title').innerHTML = esc(ri.title);
        insightCard.querySelector('.insight-body').innerHTML = esc(ri.body);

        if (ri.actions && ri.actions.length) {
          var actionsEl = insightCard.querySelector('.insight-actions');
          if (actionsEl) {
            actionsEl.innerHTML = ri.actions.map(function (a) {
              return insightActionButton(a);
            }).join('');
          }
        }
      }
    }
  }

  function renderAll(data) {
    renderAttention(data);
    renderInitiatives(data);
    renderCalendar(data);
    renderPeople(data);

    // Re-linkify task IDs after content replacement
    linkifyTaskIds();
  }

  function linkifyTaskIds() {
    document.querySelectorAll('.it-id').forEach(function (el) {
      var m = el.textContent.match(/#(\d+)/);
      if (m && !el.querySelector('.task-link')) {
        el.innerHTML = '<a href="#" class="task-link" data-task-id="' + m[1] +
          '" onclick="openTaskPanel(' + m[1] + ');return false">' + el.textContent + '</a>';
      }
    });
    var containers = document.querySelectorAll(
      '.insight-body, .insight-title, .init-cos-text, .rec-text, .init-actions'
    );
    containers.forEach(function (el) {
      el.innerHTML = el.innerHTML.replace(/#(\d{2,4})(?![^<]*>)/g, function (match, id) {
        return '<a href="#" class="task-link" data-task-id="' + id +
          '" onclick="openTaskPanel(' + id + ');return false">' + match + '</a>';
      });
    });
  }

  // ── Refresh trigger ──────────────────────────────────────
  window.triggerBriefingRefresh = function () {
    showBanner('Generating fresh briefing...', 'refreshing');
    fetch('/api/briefing/refresh', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.ok) startPolling();
        else showBanner('Refresh failed: ' + (d.message || 'unknown error'), 'error');
      })
      .catch(function () {
        showBanner('Could not start refresh', 'error');
      });
  };

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      fetch('/api/briefing')
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status === 'ready' && d.content) {
            stopPolling();
            removeBanner();
            renderAll(d.content);
            updateTimestamp(d.generated_at);
          } else if (d.status === 'error') {
            stopPolling();
            showBanner('Refresh failed: ' + (d.error_message || 'unknown'), 'error');
          }
          // else still running — keep polling
        });
    }, POLL_INTERVAL);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function updateTimestamp(generated_at) {
    if (!generated_at) return;
    var dateEl = document.getElementById('briefing-date');
    if (dateEl) {
      var gen = new Date(generated_at);
      var now = new Date();
      var sameDay = gen.toDateString() === now.toDateString();
      var timeStr = gen.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
      dateEl.textContent = now.toLocaleDateString('en-US', {
        weekday: 'long', month: 'long', day: 'numeric'
      }) + (sameDay ? ' · Updated ' + timeStr : ' · Last updated ' + gen.toLocaleDateString('en-US', {
        month: 'short', day: 'numeric'
      }) + ' ' + timeStr);
    }
  }

  // ── Init ─────────────────────────────────────────────────
  function init() {
    fetch('/api/briefing')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === 'ready' && d.content) {
          renderAll(d.content);
          updateTimestamp(d.generated_at);
          if (d.is_stale) {
            showBanner(
              'This briefing is from ' + new Date(d.generated_at).toLocaleDateString('en-US', {
                weekday: 'short', month: 'short', day: 'numeric'
              }) + '. Refresh for latest data.',
              'stale'
            );
          }
        } else if (d.status === 'running') {
          showBanner('Generating briefing...', 'refreshing');
          startPolling();
        } else if (d.status === 'empty' || !d.content) {
          // No briefing yet — keep mock content, show option to generate
          showBanner('No briefing generated yet. Click to generate your first briefing.', 'stale');
        } else if (d.status === 'error') {
          showBanner('Last refresh failed: ' + (d.error_message || 'unknown'), 'error');
        }
      })
      .catch(function () {
        // API not available — keep mock content silently
      });
  }

  // Run after DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
