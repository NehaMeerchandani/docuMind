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
  const csrfToken = document.getElementById('csrf-token').value;

  const fileInput = document.getElementById('chat-file-input');
  const attachBtn = document.getElementById('chat-attach-btn');
  const attachmentChip = document.getElementById('chat-attachment-chip');
  const attachmentNameEl = document.getElementById('chat-attachment-name');
  const attachmentRemoveBtn = document.getElementById('chat-attachment-remove');

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

  function clearMessages() {
    messagesEl.innerHTML = '';
  }

  function appendMessage(role, text) {
    const div = document.createElement('div');
    div.className = 'chat-bubble ' + (role === 'user' ? 'user' : 'assistant');

    const label = document.createElement('div');
    label.className = 'chat-bubble-label';
    label.textContent = role === 'user' ? 'You' : 'Assistant';

    const textEl = document.createElement('span');
    textEl.textContent = text;

    div.appendChild(label);
    div.appendChild(textEl);
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function appendNotificationBubble(text) {
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

    const url = wsScheme + '://' + wsHost + '/ws/chat/' + id + '/';
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
          // A structured result (currently only charts) sent by ChatService instead of
          // the usual text preview. It counts as a tool call, so the "answered
          // directly, no tool used" note is not shown for this turn.
          anyToolUsed = true;
          if (parsed.widget.type === 'chart') {
            renderChartWidget(assistant.wrapper, parsed.widget.data);
          }
        } else if (parsed.content) {
          assistant.textEl.textContent += parsed.content;
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (parsed.error) {
          assistant.textEl.textContent += '\n[Error: ' + parsed.error + ']';
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
          // A structured result (currently only charts) sent by ChatService instead of
          // the usual text preview. It counts as a tool call, so the "answered
          // directly, no tool used" note is not shown for this turn.
          anyToolUsed = true;
          if (parsed.widget.type === 'chart') {
            renderChartWidget(assistant.wrapper, parsed.widget.data);
          }
        } else if (parsed.content) {
          assistant.textEl.textContent += parsed.content;
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (parsed.error) {
          assistant.textEl.textContent += '\n[Error: ' + parsed.error + ']';
        }
      }
    }

    if (!anyToolUsed) {
      addNoToolEntry(assistant.toolLog);
    }
  }

  // ---------- Conversation list / history ----------

  async function loadConversations() {
    const response = await fetch(conversationsUrl);
    const body = await response.json();
    conversationListEl.innerHTML = '';

    (body.data || []).forEach(function (conv) {
      const btn = document.createElement('button');
      btn.type = 'button';
      const isActive = conv.session_id === sessionId;
      btn.className = 'chat-conversation-item' + (isActive ? ' active' : '');
      btn.textContent = conv.title || '(untitled)';
      btn.addEventListener('click', function () {
        loadConversation(conv.session_id);
      });
      conversationListEl.appendChild(btn);
    });
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
          }
        } catch (err) {
          // Malformed/legacy media row -- skip it rather than breaking history render.
        }
      });
      pendingMediaRows = [];

      assistant.textEl.textContent = message.message;
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
    input.disabled = true;

    const assistant = appendAssistantContainer();
    const wasNewConversation = !sessionId;

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

    input.disabled = false;
    input.focus();

    if (wasNewConversation) {
      loadConversations();
    }
  });

  loadConversations();
});
