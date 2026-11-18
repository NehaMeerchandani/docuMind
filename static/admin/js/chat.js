document.addEventListener('DOMContentLoaded', function () {
  const PLACEHOLDER_SESSION_ID = '00000000-0000-0000-0000-000000000000';

  const app = document.getElementById('chat-app');
  const streamUrl = app.dataset.streamUrl;
  const conversationsUrl = app.dataset.conversationsUrl;
  const messagesUrlTemplate = app.dataset.messagesUrlTemplate;
 
  const chatTransport = app.dataset.chatTransport || 'sse';
  const wsScheme = app.dataset.wsScheme || 'ws';
  const wsHost = app.dataset.wsHost || window.location.host;

  const form = document.getElementById('chat-form');
  const input = document.getElementById('chat-input');
  const messagesEl = document.getElementById('chat-messages');
  const conversationListEl = document.getElementById('conversation-list');
  const newChatBtn = document.getElementById('new-chat-btn');
  const conversationSearchEl = document.getElementById('conversation-search');
  const csrfToken = document.getElementById('csrf-token').value;

  const fileInput = document.getElementById('chat-file-input');
  const attachBtn = document.getElementById('chat-attach-btn');
  const attachmentChip = document.getElementById('chat-attachment-chip');
  const attachmentNameEl = document.getElementById('chat-attachment-name');
  const attachmentRemoveBtn = document.getElementById('chat-attachment-remove');

  const agentSelect = document.getElementById('agent-select');
  const agents = JSON.parse(document.getElementById('agents-data').textContent);
  const AGENT_STORAGE_KEY = 'documind.chat.agent';

  function storedAgentSlug() {
    try {
      return window.localStorage.getItem(AGENT_STORAGE_KEY);
    } catch (err) {
      return null;
    }
  }

  // The agent this chat page talks to. Every conversation belongs to exactly one agent,
  // so the sidebar lists only that agent's conversations, and switching agents starts a
  // fresh chat (see the agentSelect change handler).
  let agentSlug = null;
  let sessionId = null;
  let socket = null;
  let socketSessionId = null;
  let activeStreamHandler = null;
  // The file picked with the paperclip but not yet sent. Cleared on send or on remove.
  let pendingFile = null;

  function generateUuid() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    // Fallback for non-secure contexts where crypto.randomUUID isn't exposed.
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      const r = (Math.random() * 16) | 0;
      const v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  // Renders **bold** segments as <strong> without innerHTML (text stays escaped).
  // The raw text is kept on the element so streamed chunks can be appended and
  // re-rendered, even when a "**" pair is split across chunks.
  function setRichText(el, text) {
    el._raw = text;
    el.textContent = '';
    const parts = text.split(/\*\*([^*]+?)\*\*/g);
    parts.forEach(function (part, i) {
      if (!part) return;
      if (i % 2 === 1) {
        const strong = document.createElement('strong');
        strong.textContent = part;
        el.appendChild(strong);
      } else {
        el.appendChild(document.createTextNode(part));
      }
    });
  }

  function appendRichText(el, chunk) {
    setRichText(el, (el._raw || '') + chunk);
  }

  const SUGGESTED_PROMPTS = [
    'What documents do I have?',
    'Summarize this conversation',
    'Which documents are still processing?',
    'Explain what you can help me with',
  ];

  // Shown whenever the message area is empty (a new chat). Removed as soon as the first
  // message is appended, so a loaded conversation never shows it.
  function showEmptyState() {
    const agent = agents.find(function (a) { return a.slug === agentSlug; });

    const box = document.createElement('div');
    box.className = 'chat-empty-state';

    const icon = document.createElement('div');
    icon.className = 'chat-empty-icon';
    icon.textContent = '\u2726';
    box.appendChild(icon);

    const title = document.createElement('div');
    title.className = 'chat-empty-title';
    title.textContent = agent ? 'Chat with ' + agent.name : 'Start a conversation';
    box.appendChild(title);

    const sub = document.createElement('div');
    sub.className = 'chat-empty-sub';
    sub.textContent = 'Ask about your documents, summarise a file, or build a chart.';
    box.appendChild(sub);

    const chips = document.createElement('div');
    chips.className = 'chat-suggestions';
    SUGGESTED_PROMPTS.forEach(function (prompt) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chat-suggestion';
      chip.textContent = prompt;
      chip.addEventListener('click', function () {
        input.value = prompt;
        form.requestSubmit();
      });
      chips.appendChild(chip);
    });
    box.appendChild(chips);

    messagesEl.appendChild(box);
  }

  function removeEmptyState() {
    const box = messagesEl.querySelector('.chat-empty-state');
    if (box) {
      box.remove();
    }
  }

  function clearMessages() {
    messagesEl.innerHTML = '';
    showEmptyState();
  }

  function addCopyButton(bubble, textEl) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chat-copy-btn';
    btn.textContent = 'Copy';
    btn.addEventListener('click', function () {
      navigator.clipboard.writeText(textEl.textContent).then(function () {
        btn.textContent = 'Copied';
        setTimeout(function () { btn.textContent = 'Copy'; }, 1200);
      });
    });
    bubble.appendChild(btn);
  }

  function appendMessage(role, text) {
    removeEmptyState();
    const div = document.createElement('div');
    div.className = 'chat-bubble ' + (role === 'user' ? 'user' : 'assistant');

    const label = document.createElement('div');
    label.className = 'chat-bubble-label';
    label.textContent = role === 'user' ? 'You' : 'Assistant';

    const textEl = document.createElement('span');
    if (role === 'user') {
      textEl.textContent = text;
    } else {
      setRichText(textEl, text);
    }

    div.appendChild(label);
    div.appendChild(textEl);
    if (role !== 'user') {
      addCopyButton(div, textEl);
    }
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function appendNotificationBubble(text) {
    removeEmptyState();
    // Renders both a just-arrived live push and a system-type row loaded from
    // history (renderHistory below) with the same look, so there's no visual
    // difference between "just happened" and "happened while this chat was closed."
    const div = document.createElement('div');
    div.className = 'chat-bubble assistant chat-notification-bubble';

    const label = document.createElement('div');
    label.className = 'chat-bubble-label';
    label.textContent = 'Update';

    const textEl = document.createElement('span');
    textEl.textContent = text;

    div.appendChild(label);
    div.appendChild(textEl);
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function appendAssistantContainer() {
    removeEmptyState();
    const wrapper = document.createElement('div');
    wrapper.className = 'chat-bubble assistant';

    const label = document.createElement('div');
    label.className = 'chat-bubble-label';
    label.textContent = 'Assistant';

    const toolLog = document.createElement('div');
    toolLog.className = 'chat-tool-log';

    const textEl = document.createElement('span');

    wrapper.appendChild(label);
    wrapper.appendChild(toolLog);
    wrapper.appendChild(textEl);
    addCopyButton(wrapper, textEl);
    messagesEl.appendChild(wrapper);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    return { wrapper: wrapper, toolLog: toolLog, textEl: textEl };
  }

  function addToolStartEntry(toolLog, toolName) {
    const entry = document.createElement('div');
    entry.className = 'chat-tool-entry pending';
    entry.dataset.toolName = toolName;
    entry.textContent = '\u{1F527} Calling tool: ' + toolName + '...';
    toolLog.appendChild(entry);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function markToolEnded(toolLog, toolName, resultPreview) {
    const entries = toolLog.querySelectorAll(
      '.chat-tool-entry.pending[data-tool-name="' + toolName + '"]',
    );
    const entry = entries[entries.length - 1];
    if (!entry) {
      addToolResultEntry(toolLog, toolName, resultPreview);
      return;
    }
    entry.className = 'chat-tool-entry done';
    entry.textContent = '\u2705 ' + toolName + ' \u2192 ' + resultPreview;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addToolResultEntry(toolLog, toolName, resultPreview) {
    const entry = document.createElement('div');
    entry.className = 'chat-tool-entry done';
    entry.textContent = '\u2705 ' + toolName + ' \u2192 ' + resultPreview;
    toolLog.appendChild(entry);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addNoToolEntry(toolLog) {
    const entry = document.createElement('div');
    entry.className = 'chat-tool-entry no-tool';
    entry.textContent = '\u{1F9ED} Answered directly by the model \u2014 no tool used';
    toolLog.appendChild(entry);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // ---------- Chart widget rendering (Chart.js, vendored in vendor/chart.umd.js) ----------

  // One entry per dataset (series), cycled if there are more series than colors.
  // `line` is the solid stroke color; `fill` is the same color at low opacity, used
  // for the area under a line chart and (made more opaque below) for bar fills.
  const CHART_PALETTE = [
    { line: 'rgb(124, 58, 237)', fill: 'rgba(124, 58, 237, 0.18)' },
    { line: 'rgb(37, 99, 235)', fill: 'rgba(37, 99, 235, 0.18)' },
    { line: 'rgb(5, 150, 105)', fill: 'rgba(5, 150, 105, 0.18)' },
    { line: 'rgb(217, 119, 6)', fill: 'rgba(217, 119, 6, 0.18)' },
    { line: 'rgb(220, 38, 38)', fill: 'rgba(220, 38, 38, 0.18)' },
    { line: 'rgb(13, 148, 136)', fill: 'rgba(13, 148, 136, 0.18)' },
  ];
  // A pie has a single dataset but many slices, so each slice (one per label) needs its
  // own color rather than one color per dataset.
  const PIE_SLICE_COLORS = [
    'rgb(124, 58, 237)', 'rgb(37, 99, 235)', 'rgb(5, 150, 105)', 'rgb(217, 119, 6)',
    'rgb(220, 38, 38)', 'rgb(13, 148, 136)', 'rgb(219, 39, 119)', 'rgb(75, 85, 99)',
  ];
  // The types the user can switch between with the buttons on the chart card. The
  // switch redraws from the data already in the browser; it never calls the server.
  // 'scatter' is deliberately absent: the backend sends label/value arrays, not x/y
  // points, so a scatter request is shown as a bar chart (see renderChartWidget).
  const CHART_TYPE_TOGGLES = [
    { type: 'bar', label: 'Bar' },
    { type: 'line', label: 'Line' },
    { type: 'pie', label: 'Pie' },
  ];

  // 12000 -> "12,000" using the browser's locale. Non-numbers (null gaps in the data)
  // are returned unchanged so Chart.js can skip them.
  function formatNumber(value) {
    if (typeof value !== 'number') {
      return value;
    }
    return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  // Turns the server's chart JSON ({labels, datasets: [{label, data}]}) into a full
  // Chart.js config for the given chart type. Kept separate from renderChartWidget so
  // the same data can be redrawn as a different type when the user clicks a toggle.
  function buildChartJsConfig(chartData, chartType) {
    // Styling is applied per type on a copy of each dataset; the server's data values
    // and labels are passed through untouched.
    const datasets = (chartData.datasets || []).map(function (dataset, i) {
      const color = CHART_PALETTE[i % CHART_PALETTE.length];
      if (chartType === 'pie') {
        return Object.assign({}, dataset, {
          backgroundColor: PIE_SLICE_COLORS,
          borderColor: '#fff',
          borderWidth: 2,
          hoverOffset: 6,
        });
      }
      if (chartType === 'line') {
        return Object.assign({}, dataset, {
          borderColor: color.line,
          backgroundColor: color.fill,
          pointBackgroundColor: color.line,
          pointBorderColor: '#fff',
          pointRadius: 4,
          pointHoverRadius: 6,
          borderWidth: 2.5,
          tension: 0.35,
          fill: true,
        });
      }
      // bar (and the fallback for any other type): the palette's translucent fill
      // string is reused with a higher alpha so bars are solid enough to read.
      return Object.assign({}, dataset, {
        backgroundColor: color.fill.replace('0.18', '0.75'),
        borderColor: color.line,
        borderWidth: 1.5,
        borderRadius: 6,
        hoverBackgroundColor: color.line,
      });
    });

    // With more than 8 categories the x-axis labels would overlap, so they are tilted.
    const manyLabels = (chartData.labels || []).length > 8;

    return {
      type: chartType,
      data: {
        labels: chartData.labels || [],
        datasets: datasets,
      },
      options: {
        responsive: true,
        // The card's canvas box fixes the height (see .chat-widget-chart-canvas-box in
        // the template), so Chart.js must not force its own aspect ratio.
        maintainAspectRatio: false,
        // Hovering anywhere along an x position shows every series' value for it.
        interaction: { mode: 'index', intersect: false },
        animation: { duration: 500, easing: 'easeOutQuart' },
        layout: { padding: { top: 4, right: 8, bottom: 0, left: 0 } },
        plugins: {
          // A legend only helps with several series or pie slices; with one series the
          // card title already says what is plotted.
          legend: {
            display: datasets.length > 1 || chartType === 'pie',
            position: 'bottom',
            labels: { boxWidth: 12, boxHeight: 12, padding: 14, usePointStyle: true },
          },
          tooltip: {
            backgroundColor: 'rgba(17, 24, 39, 0.92)',
            padding: 10,
            cornerRadius: 8,
            titleFont: { weight: '600' },
            callbacks: {
              // Pie values are in ctx.parsed directly; bar/line values are on the y axis.
              label: function (ctx) {
                const value = chartType === 'pie' ? ctx.parsed : ctx.parsed.y;
                return ctx.dataset.label + ': ' + formatNumber(value);
              },
            },
          },
        },
        // A pie has no axes. For everything else the y axis starts at zero so bar
        // heights are not visually misleading.
        scales: chartType === 'pie' ? {} : {
          x: {
            grid: { display: false },
            ticks: {
              maxRotation: manyLabels ? 60 : 0,
              minRotation: manyLabels ? 45 : 0,
              font: { size: 11 },
            },
          },
          y: {
            beginAtZero: true,
            grid: { color: 'rgba(0, 0, 0, 0.06)' },
            ticks: {
              font: { size: 11 },
              callback: function (value) { return formatNumber(value); },
            },
          },
        },
      },
    };
  }

  // Builds the chart card (title, type buttons, canvas, optional "first N rows" note)
  // inside `container` and draws the chart. Used both for a chart that has just
  // streamed in and for one reloaded from saved history (renderHistory), so a chart
  // looks the same either way. `chartData` is the "chart" object from chart_service.py.
  function renderChartWidget(container, chartData) {
    const wrapper = document.createElement('div');
    wrapper.className = 'chat-widget-chart';

    const header = document.createElement('div');
    header.className = 'chat-widget-chart-header';

    const titleEl = document.createElement('div');
    titleEl.className = 'chat-widget-chart-title';
    titleEl.textContent = chartData.title || 'Chart';
    header.appendChild(titleEl);

    const actions = document.createElement('div');
    actions.className = 'chat-widget-chart-actions';

    // Pie is only offered when there is exactly one series; several series cannot be
    // shown as one pie.
    const supportedTypes = (chartData.datasets || []).length === 1
      ? CHART_TYPE_TOGGLES
      : CHART_TYPE_TOGGLES.filter(function (t) { return t.type !== 'pie'; });

    // Start on the type the server chose. If that type has no button (for example
    // 'scatter'), fall back to the first available one instead of drawing nothing.
    let currentType = chartData.type || 'bar';
    if (!supportedTypes.some(function (t) { return t.type === currentType; })) {
      currentType = supportedTypes[0].type;
    }

    const toggleButtons = {};
    supportedTypes.forEach(function (t) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chat-chart-toggle-btn' + (t.type === currentType ? ' active' : '');
      btn.textContent = t.label;
      btn.addEventListener('click', function () {
        currentType = t.type;
        Object.keys(toggleButtons).forEach(function (key) {
          toggleButtons[key].classList.toggle('active', key === currentType);
        });
        // Chart.js cannot change a chart's type in place, so the old instance is
        // destroyed (releasing its canvas) and a new one is built from the same data.
        // `chartInstance` and `canvas` are declared further down; this handler only
        // runs on a later click, by which point both exist.
        chartInstance.destroy();
        chartInstance = new Chart(canvas.getContext('2d'), buildChartJsConfig(chartData, currentType));
      });
      toggleButtons[t.type] = btn;
      actions.appendChild(btn);
    });

    header.appendChild(actions);
    wrapper.appendChild(header);

    const canvasBox = document.createElement('div');
    canvasBox.className = 'chat-widget-chart-canvas-box';
    const canvas = document.createElement('canvas');
    canvasBox.appendChild(canvas);
    wrapper.appendChild(canvasBox);

    container.appendChild(wrapper);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    // eslint-disable-next-line no-undef -- Chart is the global from vendor/chart.umd.js
    let chartInstance = new Chart(canvas.getContext('2d'), buildChartJsConfig(chartData, currentType));

    // chart_service.py plots at most MAX_CHART_POINTS rows; say so when rows were cut.
    if (chartData.truncated) {
      const note = document.createElement('div');
      note.className = 'chat-widget-chart-note';
      note.textContent = 'Showing the first ' + (chartData.labels || []).length +
        ' of ' + formatNumber(chartData.total_rows) + ' rows.';
      wrapper.appendChild(note);
    }
  }

  // ---------- Action buttons and the task table ----------
  // Every button carries `text`; clicking sends it as the user's next message, exactly as
  // if it had been typed, so the assistant handles it like any other request.

  function sendChatText(text) {
    if (input.disabled) {
      return;
    }
    input.value = text;
    form.requestSubmit();
  }

  function renderWidget(container, widget) {
    if (widget.type === 'chart') {
      renderChartWidget(container, widget.data);
    } else if (widget.type === 'task_table') {
      renderTaskTable(container, widget.data, false);
    } else if (widget.type === 'actions') {
      renderActionButtons(container, widget.data.buttons || []);
    }
  }

  function makeActionButton(spec, disabled, onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chat-action-btn ' + (spec.style || 'secondary');
    button.textContent = spec.label;
    button.title = spec.text;
    button.disabled = !!disabled;
    button.addEventListener('click', onClick);
    return button;
  }

  function renderActionButtons(container, buttons) {
    if (!buttons.length) {
      return;
    }
    const group = document.createElement('div');
    group.className = 'chat-action-group';
    const all = [];
    buttons.forEach(function (spec) {
      const button = makeActionButton(spec, false, function () {
        all.forEach(function (b) { b.disabled = true; });
        button.classList.add('chosen');
        sendChatText(spec.text);
      });
      all.push(button);
      group.appendChild(button);
    });
    container.appendChild(group);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // What a row's action codes (chat/services/task_service.py) turn into. `text` is sent as
  // the user's message when the button is clicked.
  const TASK_ACTIONS = {
    process: { label: 'Process it', style: 'primary', text: function (id) { return 'Process document ' + id; } },
    save: { label: 'Save to knowledge base', style: 'success', text: function (id) { return 'Save document ' + id + ' to the knowledge base'; } },
    pause: { label: 'Pause', style: 'warning', text: function (id) { return 'Pause processing of document ' + id; } },
    resume: { label: 'Resume', style: 'primary', text: function (id) { return 'Resume processing of document ' + id; } },
    cancel: { label: 'Cancel', style: 'danger', text: function (id) { return 'Cancel processing of document ' + id; } },
    retry: { label: 'Retry', style: 'primary', text: function (id) { return 'Retry saving document ' + id + ' to the knowledge base'; } },
    summary: { label: 'View summary', style: 'soft', text: function (id) { return 'Show the summary of document ' + id; } }
  };

  // Filter chips. A row's status_kind is ok (in knowledge base) | run (running or
  // paused-in-progress) | warn (paused) | fail | idle (pending).
  const TASK_FILTERS = [
    { key: 'todo', label: 'To do', match: function (k) { return k !== 'ok'; } },
    { key: 'pending', label: 'Pending', match: function (k) { return k === 'idle'; } },
    { key: 'running', label: 'Running', match: function (k) { return k === 'run' || k === 'warn'; } },
    { key: 'failed', label: 'Failed', match: function (k) { return k === 'fail'; } },
    { key: 'done', label: 'Done', match: function (k) { return k === 'ok'; } },
    { key: 'all', label: 'All', match: function () { return true; } }
  ];

  function renderTaskTable(container, data, readonly) {
    const rows = data.rows || [];
    // Let the bubble use the full width of the conversation column while it holds a table.
    container.classList.add('has-table');
    const card = document.createElement('div');
    card.className = 'chat-widget-table';

    const header = document.createElement('div');
    header.className = 'chat-widget-table-header';
    const titleBox = document.createElement('div');
    const title = document.createElement('div');
    title.className = 'chat-widget-table-title';
    title.textContent = data.title || 'Tasks';
    const scope = document.createElement('div');
    scope.className = 'chat-widget-table-scope';
    scope.textContent = data.scope_label || '';
    titleBox.appendChild(title);
    titleBox.appendChild(scope);
    header.appendChild(titleBox);
    if (data.switches && data.switches.length) {
      // Admins: Mine / <this company> / All companies. The active one is highlighted;
      // the others send their text, which asks for that view.
      const seg = document.createElement('div');
      seg.className = 'chat-seg';
      data.switches.forEach(function (option) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = option.label;
        btn.title = option.text;
        btn.className = option.active ? 'active' : '';
        btn.disabled = !!readonly || !!option.active;
        btn.addEventListener('click', function () { sendChatText(option.text); });
        seg.appendChild(btn);
      });
      header.appendChild(seg);
    }
    card.appendChild(header);

    if (data.notice) {
      const notice = document.createElement('div');
      notice.className = 'chat-widget-table-notice';
      notice.textContent = data.notice;
      card.appendChild(notice);
    }

    // Toolbar: filter chips (with counts) and a search box.
    const toolbar = document.createElement('div');
    toolbar.className = 'chat-widget-table-toolbar';
    const chips = document.createElement('div');
    chips.className = 'chat-chip-row';
    const search = document.createElement('input');
    search.type = 'search';
    search.className = 'chat-widget-table-search';
    search.placeholder = 'Search name or id...';
    toolbar.appendChild(chips);
    toolbar.appendChild(search);
    card.appendChild(toolbar);

    const scroller = document.createElement('div');
    scroller.className = 'chat-widget-table-scroll';
    const table = document.createElement('table');
    const head = document.createElement('thead');
    const headRow = document.createElement('tr');
    (data.columns || []).forEach(function (name) {
      const th = document.createElement('th');
      th.textContent = name;
      headRow.appendChild(th);
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = document.createElement('tbody');
    const rowEls = rows.map(function (row) {
      const tr = document.createElement('tr');
      tr.dataset.kind = row.status_kind || 'idle';
      tr.dataset.search = (row.name + ' ' + row.id + ' ' + (row.by || '')).toLowerCase();
      tr.dataset.company = row.company || '';

      // Document: name, then id and workflow ids as small copyable tags.
      const docTd = document.createElement('td');
      docTd.className = 'doc';
      const nameEl = document.createElement('div');
      nameEl.className = 'doc-name';
      nameEl.textContent = row.name;
      docTd.appendChild(nameEl);
      const meta = document.createElement('div');
      meta.className = 'doc-meta';
      const idTag = document.createElement('span');
      idTag.className = 'chat-tag';
      idTag.textContent = 'ID ' + row.id;
      meta.appendChild(idTag);
      if (row.by) {
        const byTag = document.createElement('span');
        byTag.className = 'chat-tag by';
        byTag.textContent = 'by ' + row.by;
        byTag.title = 'Uploaded by';
        meta.appendChild(byTag);
      }
      if (row.company) {
        const companyTag = document.createElement('span');
        companyTag.className = 'chat-tag company';
        companyTag.textContent = row.company;
        companyTag.title = 'Company';
        meta.appendChild(companyTag);
      }
      [['Chat workflow', row.chat_workflow_id], ['Process workflow', row.process_workflow_id]].forEach(function (pair) {
        if (!pair[1]) {
          return;
        }
        const tag = document.createElement('button');
        tag.type = 'button';
        tag.className = 'chat-tag mono';
        tag.textContent = pair[1];
        tag.title = pair[0] + ' - click to copy';
        tag.addEventListener('click', function () {
          if (navigator.clipboard) {
            navigator.clipboard.writeText(pair[1]);
          }
          tag.classList.add('copied');
          tag.title = 'Copied';
          setTimeout(function () { tag.classList.remove('copied'); tag.title = pair[0] + ' - click to copy'; }, 1200);
        });
        meta.appendChild(tag);
      });
      docTd.appendChild(meta);
      tr.appendChild(docTd);

      // Status: badge, with the summary state underneath.
      const statusTd = document.createElement('td');
      statusTd.className = 'status';
      // "Failed: reason" -> a short badge plus the reason underneath (a long reason would
      // otherwise stretch the pill into a blob).
      const colon = row.status.indexOf(': ');
      const badge = document.createElement('span');
      badge.className = 'chat-badge ' + (row.status_kind || 'idle');
      badge.textContent = colon > 0 ? row.status.slice(0, colon) : row.status;
      badge.title = row.status;
      statusTd.appendChild(badge);
      if (colon > 0) {
        const reason = document.createElement('div');
        reason.className = 'doc-reason';
        reason.textContent = row.status.slice(colon + 2);
        reason.title = row.status.slice(colon + 2);
        statusTd.appendChild(reason);
      }
      // "Embedding 190/271" -> a thin progress bar.
      const progress = /(\d+)\/(\d+)/.exec(row.status);
      if (progress && Number(progress[2]) > 0) {
        const bar = document.createElement('div');
        bar.className = 'chat-progress';
        const fill = document.createElement('div');
        fill.style.width = Math.min(100, Math.round(100 * Number(progress[1]) / Number(progress[2]))) + '%';
        bar.appendChild(fill);
        statusTd.appendChild(bar);
      }
      if (row.summary && row.summary !== '-') {
        const summaryEl = document.createElement('div');
        summaryEl.className = 'doc-summary';
        summaryEl.textContent = 'Summary: ' + row.summary.toLowerCase();
        statusTd.appendChild(summaryEl);
      }
      tr.appendChild(statusTd);

      const actionsTd = document.createElement('td');
      actionsTd.className = 'actions';
      (row.actions || []).forEach(function (code) {
        const action = TASK_ACTIONS[code];
        if (!action) {
          return;
        }
        const text = action.text(row.id);
        actionsTd.appendChild(makeActionButton(
          { label: action.label, text: text, style: action.style }, readonly,
          function () { sendChatText(text); }
        ));
      });
      if (!actionsTd.childNodes.length) {
        const none = document.createElement('span');
        none.className = 'doc-summary';
        none.textContent = 'No actions';
        actionsTd.appendChild(none);
      }
      tr.appendChild(actionsTd);

      body.appendChild(tr);
      return tr;
    });
    const emptyRow = document.createElement('tr');
    emptyRow.className = 'empty';
    const emptyTd = document.createElement('td');
    emptyTd.colSpan = 3;
    emptyTd.textContent = 'No documents match this filter.';
    emptyRow.appendChild(emptyTd);
    emptyRow.hidden = true;
    body.appendChild(emptyRow);
    table.appendChild(body);
    scroller.appendChild(table);
    card.appendChild(scroller);

    const footer = document.createElement('div');
    footer.className = 'chat-widget-table-footer';
    card.appendChild(footer);

    // Start on "To do" (everything not finished) when there is anything to do, so the
    // finished documents do not bury it; otherwise show all.
    let activeKey = rows.some(function (r) { return r.status_kind !== 'ok'; }) ? 'todo' : 'all';
    let activeCompany = '';
    const chipEls = {};
    const companyChipEls = {};

    function inCompany(tr) {
      return !activeCompany || tr.dataset.company === activeCompany;
    }

    function applyFilter() {
      const filter = TASK_FILTERS.filter(function (f) { return f.key === activeKey; })[0];
      const needle = search.value.trim().toLowerCase();
      let shown = 0;
      rowEls.forEach(function (tr) {
        const visible = inCompany(tr) && filter.match(tr.dataset.kind) &&
          (!needle || tr.dataset.search.indexOf(needle) !== -1);
        tr.hidden = !visible;
        if (visible) {
          shown += 1;
        }
      });
      emptyRow.hidden = shown !== 0;
      // Status counts follow the company filter, so they always add up to what you see.
      TASK_FILTERS.forEach(function (f) {
        const count = rowEls.filter(function (tr) { return inCompany(tr) && f.match(tr.dataset.kind); }).length;
        chipEls[f.key].textContent = f.label + ' (' + count + ')';
        chipEls[f.key].classList.toggle('active', f.key === activeKey);
      });
      Object.keys(companyChipEls).forEach(function (name) {
        companyChipEls[name].classList.toggle('active', name === activeCompany);
      });
      footer.textContent = 'Showing ' + shown + ' of ' + rows.length + '. ' +
        (readonly ? 'Snapshot from ' : 'As of ') + (data.generated_at || '') +
        (readonly ? ' - ask again for a live view.' : '.') +
        (data.truncated ? ' Only the newest ' + rows.length + ' documents are listed.' : '');
    }

    TASK_FILTERS.forEach(function (f) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chat-chip';
      chip.addEventListener('click', function () {
        activeKey = f.key;
        applyFilter();
      });
      chipEls[f.key] = chip;
      chips.appendChild(chip);
    });

    // All-companies view: a second row of chips filters by company.
    const companyCounts = {};
    rows.forEach(function (r) {
      if (r.company) {
        companyCounts[r.company] = (companyCounts[r.company] || 0) + 1;
      }
    });
    const companyNames = Object.keys(companyCounts).sort();
    if (companyNames.length) {
      const companyRow = document.createElement('div');
      companyRow.className = 'chat-chip-row chat-company-row';
      const caption = document.createElement('span');
      caption.className = 'chat-chip-caption';
      caption.textContent = 'Company';
      companyRow.appendChild(caption);
      [''].concat(companyNames).forEach(function (name) {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'chat-chip company';
        chip.textContent = name ? name + ' (' + companyCounts[name] + ')' : 'All (' + rows.length + ')';
        chip.addEventListener('click', function () {
          activeCompany = name;
          applyFilter();
        });
        companyChipEls[name] = chip;
        companyRow.appendChild(chip);
      });
      toolbar.parentNode.insertBefore(companyRow, toolbar);
    }

    search.addEventListener('input', applyFilter);
    applyFilter();

    container.appendChild(card);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // ---------- WebSocket transport ----------

  function onSocketMessage(event) {
    const parsed = JSON.parse(event.data);

    if (parsed.document_notification) {
      // Can arrive at any time this socket is open, independent of whether a
      // question is currently in flight -- see DocumentService._notify_watchers
      // (document/services/document_service.py), which pushes this the moment a
      // watched document finishes processing.
      appendNotificationBubble(parsed.document_notification.message);
      return;
    }

    if (activeStreamHandler) {
      activeStreamHandler(parsed);
    }
  }

  function connectSocket(id) {
    if (socket && socketSessionId === id && socket.readyState === WebSocket.OPEN) {
      return Promise.resolve(socket);
    }
    if (socket) {
      socket.close();
    }

    const url = wsScheme + '://' + wsHost + '/ws/chat/' + id + '/?agent=' + encodeURIComponent(agentSlug || '');
    socket = new WebSocket(url);
    socketSessionId = id;

    return new Promise(function (resolve, reject) {
      socket.addEventListener('open', function () {
        resolve(socket);
      });
      socket.addEventListener('message', onSocketMessage);
      socket.addEventListener('error', function () {
        reject(new Error('Could not connect to the chat websocket.'));
      });
      socket.addEventListener('close', function () {
        if (socketSessionId === id) {
          socket = null;
          socketSessionId = null;
        }
      });
    });
  }

  function sendOverWebSocket(question, assistant) {
    return new Promise(function (resolve) {
      let anyToolUsed = false;

      activeStreamHandler = function (parsed) {
        if (parsed.tool_start) {
          anyToolUsed = true;
          addToolStartEntry(assistant.toolLog, parsed.tool_start.name);
        } else if (parsed.tool_end) {
          markToolEnded(assistant.toolLog, parsed.tool_end.name, parsed.tool_end.result_preview);
        } else if (parsed.widget) {
          // A structured result (chart, task table, action buttons) sent by ChatService instead of
          // the usual text preview. It counts as a tool call, so the "answered
          // directly, no tool used" note is not shown for this turn.
          anyToolUsed = true;
          renderWidget(assistant.wrapper, parsed.widget);
        } else if (parsed.content) {
          appendRichText(assistant.textEl, parsed.content);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (parsed.error) {
          appendRichText(assistant.textEl, '\n[Error: ' + parsed.error + ']');
        } else if (parsed.done) {
          activeStreamHandler = null;
          if (!anyToolUsed) {
            addNoToolEntry(assistant.toolLog);
          }
          resolve();
        }
      };

      socket.send(JSON.stringify({ question: question }));
    });
  }

  // ---------- SSE transport (original /chat/stream/ endpoint, unchanged) ----------

  // `file` is optional. With a file the request is multipart (so the raw bytes can be
  // uploaded); without one it is the original JSON request, unchanged.
  async function sendOverSse(question, assistant, file) {
    let anyToolUsed = false;

    let fetchOptions;
    if (file) {
      // multipart/form-data -- deliberately no Content-Type header here, the browser
      // sets the correct boundary itself. AdminChatStreamView.post (chat/admin.py)
      // branches on request.FILES to tell this apart from the plain-JSON path below.
      const formData = new FormData();
      formData.append('question', question);
      formData.append('agent', agentSlug || '');
      if (sessionId) {
        formData.append('session_id', sessionId);
      }
      formData.append('file', file);
      fetchOptions = {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken },
        body: formData,
      };
    } else {
      fetchOptions = {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrfToken,
        },
        body: JSON.stringify({
          question: question,
          session_id: sessionId,
          agent: agentSlug,
        }),
      };
    }

    const response = await fetch(streamUrl, fetchOptions);

    if (!sessionId) {
      const newSessionId = response.headers.get('X-Session-Id');
      if (newSessionId) {
        sessionId = newSessionId;
      }
    }

    if (!response.ok) {
      const errorBody = await response.json();
      assistant.textEl.textContent = 'Error: ' + (errorBody.error || 'Something went wrong.');
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const result = await reader.read();
      if (result.done) {
        break;
      }

      buffer += decoder.decode(result.value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop();

      for (const event of events) {
        if (!event.startsWith('data: ')) {
          continue;
        }

        const payload = event.slice('data: '.length);
        if (payload === '[DONE]') {
          continue;
        }

        const parsed = JSON.parse(payload);
        if (parsed.tool_start) {
          anyToolUsed = true;
          addToolStartEntry(assistant.toolLog, parsed.tool_start.name);
        } else if (parsed.tool_end) {
          markToolEnded(assistant.toolLog, parsed.tool_end.name, parsed.tool_end.result_preview);
        } else if (parsed.widget) {
          // A structured result (chart, task table, action buttons) sent by ChatService instead of
          // the usual text preview. It counts as a tool call, so the "answered
          // directly, no tool used" note is not shown for this turn.
          anyToolUsed = true;
          renderWidget(assistant.wrapper, parsed.widget);
        } else if (parsed.content) {
          appendRichText(assistant.textEl, parsed.content);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (parsed.error) {
          appendRichText(assistant.textEl, '\n[Error: ' + parsed.error + ']');
        }
      }
    }

    if (!anyToolUsed) {
      addNoToolEntry(assistant.toolLog);
    }
  }

  // ---------- Conversation list / history ----------

  let loadedConversations = [];

  function conversationGroup(createdAt) {
    const startOfToday = new Date();
    startOfToday.setHours(0, 0, 0, 0);
    const day = 24 * 60 * 60 * 1000;
    const age = startOfToday - new Date(createdAt);
    if (age <= 0) return 'Today';
    if (age <= day) return 'Yesterday';
    if (age <= 7 * day) return 'Previous 7 days';
    return 'Older';
  }

  function renderConversationList() {
    const query = conversationSearchEl.value.trim().toLowerCase();
    conversationListEl.innerHTML = '';

    let lastGroup = null;
    loadedConversations.forEach(function (conv) {
      const title = conv.title || '(untitled)';
      if (query && title.toLowerCase().indexOf(query) === -1) {
        return;
      }

      const group = conversationGroup(conv.created_at);
      if (group !== lastGroup) {
        const label = document.createElement('div');
        label.className = 'chat-group-label';
        label.textContent = group;
        conversationListEl.appendChild(label);
        lastGroup = group;
      }

      const btn = document.createElement('button');
      btn.type = 'button';
      const isActive = conv.session_id === sessionId;
      btn.className = 'chat-conversation-item' + (isActive ? ' active' : '');
      btn.textContent = title;
      btn.title = title;
      btn.addEventListener('click', function () {
        loadConversation(conv.session_id);
      });
      conversationListEl.appendChild(btn);
    });
  }

  conversationSearchEl.addEventListener('input', renderConversationList);

  async function loadConversations() {
    const response = await fetch(conversationsUrl + '?agent=' + encodeURIComponent(agentSlug || ''));
    const body = await response.json();
    loadedConversations = body.data || [];
    renderConversationList();
  }

  async function loadConversation(id) {
    sessionId = id;
    clearMessages();

    const url = messagesUrlTemplate.replace(PLACEHOLDER_SESSION_ID, id);
    const response = await fetch(url);
    const body = await response.json();

    if (body.success) {
      renderHistory(body.data.messages || []);
    }

    if (chatTransport === 'websocket') {
      // Reconnecting when an existing conversation is opened (rather than only on
      // first send) is what lets a document-processing notification arrive live
      // while the user is just reading this conversation, not actively typing.
      try {
        await connectSocket(id);
      } catch (err) {
        // Non-fatal -- history above already rendered; sending a new message will
        // retry the connection.
      }
    }

    loadConversations();
    input.focus();
  }

  function renderHistory(messages) {
    // Tool rows (and media/chart rows) for a given assistant turn are saved right
    // before that turn's final text reply, so we buffer them and flush into the same
    // bubble once the text row for that turn arrives (mirrors how
    // appendAssistantContainer groups them live).
    let pendingToolRows = [];
    let pendingMediaRows = [];

    messages.forEach(function (message) {
      if (message.message_type === 'tool') {
        pendingToolRows.push(message);
        return;
      }

      if (message.message_type === 'media') {
        pendingMediaRows.push(message);
        return;
      }

      if (message.message_type === 'system') {
        appendNotificationBubble(message.message);
        return;
      }

      if (message.message_type !== 'text') {
        return;
      }

      if (message.role === 'user') {
        appendMessage('user', message.message);
        return;
      }

      const assistant = appendAssistantContainer();
      pendingToolRows.forEach(function (toolRow) {
        if (toolRow.tool_name === 'no_tool') {
          addNoToolEntry(assistant.toolLog);
        } else {
          addToolResultEntry(assistant.toolLog, toolRow.tool_name, toolRow.message);
        }
      });
      pendingToolRows = [];

      pendingMediaRows.forEach(function (mediaRow) {
        try {
          const parsed = JSON.parse(mediaRow.message);
          if (parsed.chart) {
            renderChartWidget(assistant.wrapper, parsed.chart);
          } else if (parsed.table) {
            // A saved table is a snapshot: its buttons are disabled so an old "Pause" is
            // never clicked on something that has since finished.
            renderTaskTable(assistant.wrapper, parsed.table, true);
          }
        } catch (err) {
          // Malformed/legacy media row -- skip it rather than breaking history render.
        }
      });
      pendingMediaRows = [];

      setRichText(assistant.textEl, message.message);
    });
  }

  // ---------- File attachment ----------

  // Keeps the pending file and the chip above the input in sync: pass a File to show
  // its name, or null to clear both.
  function setPendingFile(file) {
    pendingFile = file;
    if (file) {
      attachmentNameEl.textContent = file.name;
      attachmentChip.classList.add('visible');
    } else {
      attachmentNameEl.textContent = '';
      attachmentChip.classList.remove('visible');
    }
  }

  attachBtn.addEventListener('click', function () {
    fileInput.click();
  });

  fileInput.addEventListener('change', function () {
    const file = fileInput.files && fileInput.files[0];
    setPendingFile(file || null);
  });

  attachmentRemoveBtn.addEventListener('click', function () {
    fileInput.value = '';
    setPendingFile(null);
  });

  function selectAgent(slug) {
    agentSlug = slug;
    try {
      window.localStorage.setItem(AGENT_STORAGE_KEY, slug);
    } catch (err) {
      // Remembering the last agent is a convenience only.
    }
  }

  agents.forEach(function (agent) {
    const option = document.createElement('option');
    option.value = agent.slug;
    option.textContent = agent.name;
    agentSelect.appendChild(option);
  });

  const initialAgent = agents.find(function (a) { return a.slug === storedAgentSlug(); }) || agents[0];
  if (initialAgent) {
    agentSelect.value = initialAgent.slug;
    selectAgent(initialAgent.slug);
  }

  agentSelect.addEventListener('change', function () {
    selectAgent(agentSelect.value);
    sessionId = null;
    if (socket) {
      socket.close();
    }
    clearMessages();
    loadConversations();
    input.focus();
  });

  function resizeInput() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  }

  input.addEventListener('input', resizeInput);
  input.addEventListener('keydown', function (event) {
    // Enter sends; Shift+Enter inserts a new line.
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  newChatBtn.addEventListener('click', function () {
    sessionId = null;
    if (socket) {
      socket.close();
    }
    clearMessages();
    loadConversations();
    input.focus();
  });

  form.addEventListener('submit', async function (event) {
    event.preventDefault();

    const question = input.value.trim();
    if (!question) {
      return;
    }

    // Typing "hello" is a deliberate shortcut to force a brand new session, even if
    // a conversation is currently open -- mirrors clicking "+ New chat" but from the
    // input box. Works for both transports: connectSocket() below will notice
    // sessionId no longer matches the currently-open socket's session and open a
    // fresh one.
    if (question.toLowerCase() === 'hello') {
      sessionId = null;
    }

    const fileToSend = pendingFile;
    setPendingFile(null);
    fileInput.value = '';

    appendMessage('user', question + (fileToSend ? ' \ud83d\udcce ' + fileToSend.name : ''));
    input.value = '';
    resizeInput();
    input.disabled = true;

    const assistant = appendAssistantContainer();
    const wasNewConversation = !sessionId;
    // Shows the "typing" dots until the first text arrives (see .typing in the template).
    assistant.wrapper.classList.add('typing');

    try {
      if (fileToSend) {
        // File uploads always go over the SSE/HTTP endpoint, regardless of
        // CHAT_TRANSPORT -- multipart/form-data (needed to carry the file's raw
        // bytes) has no equivalent on the plain-JSON WebSocket protocol
        // (chat/consumers.py only ever reads {"question": ...} text frames).
        await sendOverSse(question, assistant, fileToSend);
      } else if (chatTransport === 'websocket') {
        if (!sessionId) {
          // Lazily minted client-side, mirroring the SSE flow's lazy server-side
          // creation -- no Conversation row exists until the first message is
          // actually sent (see chat/consumers.py's ChatConsumer.connect, which
          // get_or_create's it once this socket connects).
          sessionId = generateUuid();
        }
        await connectSocket(sessionId);
        await sendOverWebSocket(question, assistant);
      } else {
        await sendOverSse(question, assistant);
      }
    } catch (err) {
      assistant.textEl.textContent = 'Error: ' + err.message;
    }

    assistant.wrapper.classList.remove('typing');
    input.disabled = false;
    input.focus();

    if (wasNewConversation) {
      loadConversations();
    }
  });

  showEmptyState();
  loadConversations();
});
