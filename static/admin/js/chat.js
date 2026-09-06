document.addEventListener('DOMContentLoaded', function () {
  const PLACEHOLDER_SESSION_ID = '00000000-0000-0000-0000-000000000000';

  const app = document.getElementById('chat-app');
  const streamUrl = app.dataset.streamUrl;
  const conversationsUrl = app.dataset.conversationsUrl;
  const messagesUrlTemplate = app.dataset.messagesUrlTemplate;

  const form = document.getElementById('chat-form');
  const input = document.getElementById('chat-input');
  const messagesEl = document.getElementById('chat-messages');
  const conversationListEl = document.getElementById('conversation-list');
  const newChatBtn = document.getElementById('new-chat-btn');
  const csrfToken = document.getElementById('csrf-token').value;

  let sessionId = null;

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

    loadConversations();
    input.focus();
  }

  function renderHistory(messages) {
    // Tool rows for a given assistant turn are saved right before that turn's final
    // text reply, so we buffer them and flush into the same bubble once the text row
    // for that turn arrives (mirrors how appendAssistantContainer groups them live).
    let pendingToolRows = [];

    messages.forEach(function (message) {
      if (message.message_type === 'tool') {
        pendingToolRows.push(message);
        return;
      }

      if (message.message_type !== 'text') {
        return;
      }

      if (message.sender === 'user') {
        appendMessage('user', message.content);
        return;
      }

      const assistant = appendAssistantContainer();
      pendingToolRows.forEach(function (toolRow) {
        if (toolRow.tool_name === 'no_tool') {
          addNoToolEntry(assistant.toolLog);
        } else {
          addToolResultEntry(assistant.toolLog, toolRow.tool_name, toolRow.content);
        }
      });
      pendingToolRows = [];
      assistant.textEl.textContent = message.content;
    });
  }

  newChatBtn.addEventListener('click', function () {
    sessionId = null;
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

    if (question.toLowerCase() === 'hello') {
      sessionId = null;
    }

    appendMessage('user', question);
    input.value = '';
    input.disabled = true;

    const assistant = appendAssistantContainer();
    const assistantEl = assistant.textEl;
    const wasNewConversation = !sessionId;
    let anyToolUsed = false;

    const response = await fetch(streamUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken,
      },
      body: JSON.stringify({
        question: question,
        session_id: sessionId,
      }),
    });

    if (!sessionId) {
      const newSessionId = response.headers.get('X-Session-Id');
      if (newSessionId) {
        sessionId = newSessionId;
      }
    }

    if (!response.ok) {
      const errorBody = await response.json();
      assistantEl.textContent = 'Error: ' + (errorBody.error || 'Something went wrong.');
      input.disabled = false;
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
        } else if (parsed.content) {
          assistantEl.textContent += parsed.content;
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (parsed.error) {
          assistantEl.textContent += '\n[Error: ' + parsed.error + ']';
        }
      }
    }

    if (!anyToolUsed) {
      addNoToolEntry(assistant.toolLog);
    }

    input.disabled = false;
    input.focus();

    if (wasNewConversation) {
      loadConversations();
    }
  });

  loadConversations();
});
