document.addEventListener('DOMContentLoaded', function () {
  const app = document.getElementById('workflow-app');
  const agentsUrl = app.dataset.agentsUrl;
  const detailUrlTemplate = app.dataset.detailUrlTemplate;
  const saveUrl = app.dataset.saveUrl;

  const agentSelect = document.getElementById('agent-select');
  const nameInput = document.getElementById('agent-name');
  const descriptionInput = document.getElementById('agent-description');
  const statusEl = document.getElementById('workflow-status');
  const canvasEl = document.getElementById('workflow-canvas');
  const nodesLayer = document.getElementById('workflow-nodes');
  const edgesLayer = document.getElementById('workflow-edges');
  const csrfToken = document.getElementById('csrf-token').value;
  const toolChoices = JSON.parse(document.getElementById('tool-choices').textContent); // [[id, label], ...]

  const NODE_WIDTH = 260;
  const NODE_HEADER_HEIGHT = 40;

  let currentAgentId = null;
  let nodes = [];
  let edges = [];
  let nextId = 1;

  function newId(prefix) {
    return prefix + '-' + (nextId++) + '-' + Date.now().toString(36);
  }

  function showStatus(message, isError) {
    statusEl.textContent = message;
    statusEl.className = 'workflow-status ' + (isError ? 'error' : 'success');
  }

  function toolLabel(toolId) {
    const match = toolChoices.find(function (choice) { return choice[0] === toolId; });
    return match ? match[1] : toolId;
  }

  // ---------- Empty / New workflow ----------

  function resetToBlank() {
    currentAgentId = null;
    nameInput.value = '';
    descriptionInput.value = '';
    nodes = [
      { id: newId('start'), type: 'start', position: { x: 60, y: 140 }, data: {} },
    ];
    edges = [];
    render();
    showStatus('New workflow. Add a step node and connect it to Start.', false);
  }

  // ---------- Load agent list ----------

  async function loadAgentOptions() {
    const response = await fetch(agentsUrl);
    const body = await response.json();
    agentSelect.innerHTML = '<option value="">-- select an agent --</option>';
    (body.data || []).forEach(function (agent) {
      const option = document.createElement('option');
      option.value = agent.id;
      option.textContent = agent.name;
      agentSelect.appendChild(option);
    });
  }

  async function loadAgent(agentId) {
    const url = detailUrlTemplate.replace('/0/', '/' + agentId + '/');
    const response = await fetch(url);
    const body = await response.json();

    if (!body.success) {
      showStatus(body.error || 'Could not load this agent.', true);
      return;
    }

    currentAgentId = body.data.id;
    nameInput.value = body.data.name;
    descriptionInput.value = body.data.description || '';

    const workflow = body.data.workflow_json || {};
    nodes = (workflow.nodes || []).map(function (node) {
      return {
        id: node.id,
        type: node.type,
        position: { x: (node.position || {}).x || 0, y: (node.position || {}).y || 0 },
        data: node.data || {},
      };
    });
    edges = (workflow.edges || []).map(function (edge) {
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        data: edge.data || {},
      };
    });

    render();
    showStatus('Loaded "' + body.data.name + '".', false);
  }

  // ---------- Save ----------

  function serializeWorkflow() {
    return {
      nodes: nodes.map(function (node) {
        return { id: node.id, type: node.type, position: node.position, data: node.data };
      }),
      edges: edges.map(function (edge) {
        return { id: edge.id, source: edge.source, target: edge.target, data: edge.data };
      }),
    };
  }

  async function saveWorkflow() {
    const name = nameInput.value.trim();
    if (!name) {
      showStatus('Give this agent a name before saving.', true);
      return;
    }

    const payload = {
      id: currentAgentId,
      name: name,
      description: descriptionInput.value,
      workflow_json: serializeWorkflow(),
    };

    const response = await fetch(saveUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
      body: JSON.stringify(payload),
    });
    const body = await response.json();

    if (!body.success) {
      showStatus(body.error || 'Could not save this workflow.', true);
      return;
    }

    currentAgentId = body.data.id;
    showStatus('Saved.', false);
    loadAgentOptions();
  }

  // ---------- Node CRUD ----------

  function addNode() {
    const hasStart = nodes.some(function (node) { return node.type === 'start'; });
    const offset = nodes.length * 30;

    nodes.push({
      id: newId('node'),
      type: hasStart ? 'step' : 'start',
      position: { x: 60 + offset, y: 140 + offset },
      data: {},
    });
    render();
  }

  function removeNode(nodeId) {
    nodes = nodes.filter(function (node) { return node.id !== nodeId; });
    edges = edges.filter(function (edge) { return edge.source !== nodeId && edge.target !== nodeId; });
    render();
  }

  function removeEdge(edgeId) {
    edges = edges.filter(function (edge) { return edge.id !== edgeId; });
    render();
  }

  function setNodeType(nodeId, newType) {
    const node = nodes.find(function (n) { return n.id === nodeId; });
    if (!node) return;
    node.type = newType;
    if (newType !== 'step') {
      node.data = {};
    }
    render();
  }

  // ---------- Rendering ----------

  function render() {
    renderNodes();
    renderEdges();
  }

  function renderNodes() {
    nodesLayer.innerHTML = '';

    nodes.forEach(function (node) {
      const el = document.createElement('div');
      el.className = 'workflow-node';
      el.style.left = node.position.x + 'px';
      el.style.top = node.position.y + 'px';
      el.dataset.nodeId = node.id;

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'workflow-node-remove';
      removeBtn.textContent = '\u00d7';
      removeBtn.title = 'Remove node';
      removeBtn.addEventListener('click', function (event) {
        event.stopPropagation();
        removeNode(node.id);
      });
      el.appendChild(removeBtn);

      el.appendChild(buildTypeField(node));

      if (node.type === 'step') {
        el.appendChild(buildNameField(node));
        el.appendChild(buildToolsField(node));
      }

      if (node.type !== 'start') {
        const inPort = document.createElement('div');
        inPort.className = 'workflow-node-port workflow-node-port--in';
        inPort.dataset.nodeId = node.id;
        inPort.dataset.portType = 'in';
        el.appendChild(inPort);
      }
      if (node.type !== 'end') {
        const outPort = document.createElement('div');
        outPort.className = 'workflow-node-port workflow-node-port--out';
        outPort.dataset.nodeId = node.id;
        outPort.dataset.portType = 'out';
        outPort.addEventListener('mousedown', function (event) {
          event.stopPropagation();
          startConnecting(node.id, event);
        });
        el.appendChild(outPort);
      }

      el.addEventListener('mousedown', function (event) {
        if (['INPUT', 'SELECT', 'BUTTON'].indexOf(event.target.tagName) !== -1) {
          return;
        }
        startDraggingNode(node, el, event);
      });

      nodesLayer.appendChild(el);
    });
  }

  function buildTypeField(node) {
    const wrapper = document.createElement('div');
    wrapper.className = 'workflow-node-field';

    const label = document.createElement('label');
    label.textContent = 'Type:';
    wrapper.appendChild(label);

    const select = document.createElement('select');
    ['start', 'step', 'end'].forEach(function (type) {
      const option = document.createElement('option');
      option.value = type;
      option.textContent = type.toUpperCase();
      if (type === node.type) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener('change', function () {
      setNodeType(node.id, select.value);
    });
    wrapper.appendChild(select);

    return wrapper;
  }

  function buildNameField(node) {
    const wrapper = document.createElement('div');
    wrapper.className = 'workflow-node-field';

    const label = document.createElement('label');
    label.textContent = 'Name (Langfuse prompt slug):';
    wrapper.appendChild(label);

    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = 'e.g. rag-system-prompt';
    input.value = node.data.name || '';
    input.addEventListener('input', function () {
      node.data.name = input.value;
    });
    wrapper.appendChild(input);

    return wrapper;
  }

  function buildToolsField(node) {
    const wrapper = document.createElement('div');
    wrapper.className = 'workflow-node-field';

    const label = document.createElement('label');
    label.textContent = 'Tools:';
    wrapper.appendChild(label);

    const select = document.createElement('select');
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '+ add a tool...';
    select.appendChild(placeholder);

    toolChoices.forEach(function (choice) {
      const option = document.createElement('option');
      option.value = choice[0];
      option.textContent = choice[1];
      select.appendChild(option);
    });

    select.addEventListener('change', function () {
      const toolId = select.value;
      if (!toolId) return;
      node.data.tools = node.data.tools || [];
      if (node.data.tools.indexOf(toolId) === -1) {
        node.data.tools.push(toolId);
      }
      select.value = '';
      render();
    });
    wrapper.appendChild(select);

    const chipList = document.createElement('div');
    chipList.className = 'workflow-node-tools-list';
    (node.data.tools || []).forEach(function (toolId) {
      const chip = document.createElement('span');
      chip.className = 'workflow-tool-chip';

      const text = document.createElement('span');
      text.textContent = toolLabel(toolId);
      chip.appendChild(text);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.textContent = '\u00d7';
      removeBtn.addEventListener('click', function () {
        node.data.tools = node.data.tools.filter(function (id) { return id !== toolId; });
        render();
      });
      chip.appendChild(removeBtn);

      chipList.appendChild(chip);
    });
    wrapper.appendChild(chipList);

    return wrapper;
  }

  // ---------- Edges (SVG) ----------

  function nodeById(nodeId) {
    return nodes.find(function (node) { return node.id === nodeId; });
  }

  function estimateNodeHeight(node) {
    if (node.type === 'step') {
      const toolCount = (node.data.tools || []).length;
      return NODE_HEADER_HEIGHT + 60 + 40 + (toolCount > 0 ? 30 : 10);
    }
    return NODE_HEADER_HEIGHT + 50;
  }

  function portPosition(nodeId, portType) {
    const node = nodeById(nodeId);
    if (!node) return { x: 0, y: 0 };
    const height = estimateNodeHeight(node);
    return {
      x: node.position.x + (portType === 'out' ? NODE_WIDTH : 0),
      y: node.position.y + height / 2,
    };
  }

  function edgePathD(from, to) {
    const dx = Math.max(60, (to.x - from.x) / 2);
    return 'M ' + from.x + ' ' + from.y
      + ' C ' + (from.x + dx) + ' ' + from.y + ', '
      + (to.x - dx) + ' ' + to.y + ', '
      + to.x + ' ' + to.y;
  }

  function conditionLabel(edge) {
    const condition = (edge.data || {}).condition || 'always';
    if (condition === 'always') return '';
    return condition.replace('tool_called:', 'if used: ').replace('tool_not_called:', 'if not used: ');
  }

  function renderEdges() {
    edgesLayer.innerHTML = '';

    edges.forEach(function (edge) {
      const from = portPosition(edge.source, 'out');
      const to = portPosition(edge.target, 'in');

      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', edgePathD(from, to));
      path.setAttribute('class', 'workflow-edge-path');
      path.dataset.edgeId = edge.id;
      path.addEventListener('click', function (event) {
        openConditionPopup(edge, event);
      });
      edgesLayer.appendChild(path);

      const label = conditionLabel(edge);
      if (label) {
        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('x', (from.x + to.x) / 2);
        text.setAttribute('y', (from.y + to.y) / 2 - 6);
        text.setAttribute('class', 'workflow-edge-label');
        text.setAttribute('text-anchor', 'middle');
        text.textContent = label;
        edgesLayer.appendChild(text);
      }
    });

    if (dragLine) {
      edgesLayer.appendChild(dragLine);
    }
  }

  // ---------- Node dragging ----------

  function startDraggingNode(node, el, downEvent) {
    el.classList.add('dragging');
    const canvasRect = canvasEl.getBoundingClientRect();
    const startX = downEvent.clientX;
    const startY = downEvent.clientY;
    const originX = node.position.x;
    const originY = node.position.y;

    function onMove(moveEvent) {
      const dx = moveEvent.clientX - startX;
      const dy = moveEvent.clientY - startY;
      node.position.x = Math.max(0, originX + dx);
      node.position.y = Math.max(0, originY + dy);
      el.style.left = node.position.x + 'px';
      el.style.top = node.position.y + 'px';
      renderEdges();
    }

    function onUp() {
      el.classList.remove('dragging');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }

  // ---------- Edge creation by dragging from a port ----------

  let dragLine = null;
  let connectingFromNodeId = null;

  function startConnecting(sourceNodeId, downEvent) {
    connectingFromNodeId = sourceNodeId;
    dragLine = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    dragLine.setAttribute('class', 'workflow-edge-path');
    dragLine.setAttribute('stroke-dasharray', '4,4');
    edgesLayer.appendChild(dragLine);

    function onMove(moveEvent) {
      const canvasRect = canvasEl.getBoundingClientRect();
      const from = portPosition(sourceNodeId, 'out');
      const to = {
        x: moveEvent.clientX - canvasRect.left + canvasEl.scrollLeft,
        y: moveEvent.clientY - canvasRect.top + canvasEl.scrollTop,
      };
      dragLine.setAttribute('d', edgePathD(from, to));
    }

    function onUp(upEvent) {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      if (dragLine) {
        dragLine.remove();
        dragLine = null;
      }

      const target = document.elementFromPoint(upEvent.clientX, upEvent.clientY);
      if (target && target.classList.contains('workflow-node-port--in')) {
        const targetNodeId = target.dataset.nodeId;
        if (targetNodeId && targetNodeId !== connectingFromNodeId) {
          edges.push({
            id: newId('edge'),
            source: connectingFromNodeId,
            target: targetNodeId,
            data: { condition: 'always' },
          });
        }
      }
      connectingFromNodeId = null;
      render();
    }

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }

  // ---------- Conditional edge popup ----------

  function openConditionPopup(edge, clickEvent) {
    document.querySelectorAll('.workflow-edge-condition-popup').forEach(function (el) { el.remove(); });

    const sourceNode = nodeById(edge.source);
    const sourceTools = (sourceNode && sourceNode.data.tools) || [];

    const popup = document.createElement('div');
    popup.className = 'workflow-edge-condition-popup';
    const canvasRect = canvasEl.getBoundingClientRect();
    popup.style.left = (clickEvent.clientX - canvasRect.left + canvasEl.scrollLeft) + 'px';
    popup.style.top = (clickEvent.clientY - canvasRect.top + canvasEl.scrollTop) + 'px';

    const title = document.createElement('div');
    title.textContent = 'Edge condition';
    title.style.fontWeight = '600';
    popup.appendChild(title);

    const select = document.createElement('select');
    const alwaysOption = document.createElement('option');
    alwaysOption.value = 'always';
    alwaysOption.textContent = 'Always';
    select.appendChild(alwaysOption);

    sourceTools.forEach(function (toolId) {
      const calledOption = document.createElement('option');
      calledOption.value = 'tool_called:' + toolId;
      calledOption.textContent = 'If ' + toolLabel(toolId) + ' was called';
      select.appendChild(calledOption);

      const notCalledOption = document.createElement('option');
      notCalledOption.value = 'tool_not_called:' + toolId;
      notCalledOption.textContent = 'If ' + toolLabel(toolId) + ' was NOT called';
      select.appendChild(notCalledOption);
    });

    select.value = (edge.data || {}).condition || 'always';
    select.addEventListener('change', function () {
      edge.data = { condition: select.value };
      renderEdges();
    });
    popup.appendChild(select);

    const deleteBtn = document.createElement('button');
    deleteBtn.type = 'button';
    deleteBtn.className = 'workflow-btn';
    deleteBtn.style.marginTop = '8px';
    deleteBtn.style.width = '100%';
    deleteBtn.textContent = 'Delete this edge';
    deleteBtn.addEventListener('click', function () {
      removeEdge(edge.id);
      popup.remove();
    });
    popup.appendChild(deleteBtn);

    nodesLayer.appendChild(popup);

    function closeOnOutsideClick(event) {
      if (!popup.contains(event.target)) {
        popup.remove();
        document.removeEventListener('mousedown', closeOnOutsideClick);
      }
    }
    setTimeout(function () {
      document.addEventListener('mousedown', closeOnOutsideClick);
    }, 0);
  }

  // ---------- Wire up toolbar ----------

  document.getElementById('btn-new').addEventListener('click', resetToBlank);
  document.getElementById('btn-add-node').addEventListener('click', addNode);
  document.getElementById('btn-save').addEventListener('click', saveWorkflow);
  document.getElementById('btn-load').addEventListener('click', function () {
    if (!agentSelect.value) {
      showStatus('Pick an agent from the dropdown first.', true);
      return;
    }
    loadAgent(agentSelect.value);
  });

  loadAgentOptions();
  resetToBlank();
});
