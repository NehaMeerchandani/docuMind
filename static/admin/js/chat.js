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
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
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
      (body.data.messages || []).forEach(function (message) {
        if (message.message_type === 'text') {
          appendMessage(message.sender, message.content);
        }
      });
    }

    loadConversations();
    input.focus();
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

    const assistantEl = appendMessage('assistant', '');
    const wasNewConversation = !sessionId;

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
        if (parsed.content) {
          assistantEl.textContent += parsed.content;
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (parsed.error) {
          assistantEl.textContent += '\n[Error: ' + parsed.error + ']';
        }
      }
    }

    input.disabled = false;
    input.focus();

    if (wasNewConversation) {
      loadConversations();
    }
  });

  loadConversations();
});
