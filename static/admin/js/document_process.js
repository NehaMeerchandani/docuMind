document.addEventListener('DOMContentLoaded', function () {
  const STATUS_POLL_INTERVAL_MS = 1500;
  const TERMINAL_STATUSES = ['completed', 'failed'];

  function getCsrfToken() {
    const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return input ? input.value : '';
  }

  function findStatusCell(button) {
    const row = button.closest('tr');
    return row ? row.querySelector('[data-status-cell]') : null;
  }

  function findRowButtons(button) {
    const row = button.closest('tr');
    return row ? Array.from(row.querySelectorAll('.doc-process-btn')) : [button];
  }

  function setButtonsBusy(buttons, busy) {
    buttons.forEach(function (btn) {
      btn.disabled = busy;
      btn.classList.toggle('doc-process-btn--busy', busy);
    });
  }

  function showToast(message, isError) {
    const toast = document.createElement('div');
    toast.className = 'doc-process-toast' + (isError ? ' doc-process-toast--error' : '');
    toast.textContent = message;
    document.body.appendChild(toast);

    requestAnimationFrame(function () {
      toast.classList.add('doc-process-toast--visible');
    });

    setTimeout(function () {
      toast.classList.remove('doc-process-toast--visible');
      setTimeout(function () {
        toast.remove();
      }, 300);
    }, 4000);
  }

  function pollStatus(button, statusUrl) {
    fetch(statusUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (response) {
        return response.json();
      })
      .then(function (body) {
        if (!body.success) {
          return;
        }

        const buttons = findRowButtons(button);
        const statusCell = findStatusCell(button);

        if (statusCell) {
          statusCell.textContent = body.status_display;
          statusCell.className = 'doc-status-badge doc-status-badge--' + body.status;
        }

        if (TERMINAL_STATUSES.indexOf(body.status) === -1) {
          setTimeout(function () {
            pollStatus(button, statusUrl);
          }, STATUS_POLL_INTERVAL_MS);
          return;
        }

        setButtonsBusy(buttons, false);

        if (body.status === 'completed') {
          buttons.forEach(function (btn) {
            btn.textContent = 'Done';
            btn.disabled = true;
            btn.classList.add('doc-process-btn--done');
          });
        } else {
          buttons.forEach(function (btn) {
            btn.textContent = 'Retry';
          });
          if (body.error_message) {
            showToast(body.error_message, true);
          }
        }
      })
      .catch(function () {
        // A transient network hiccup while polling shouldn't spam the user; the button
        // stays busy and the next scheduled poll will simply try again.
      });
  }

  document.body.addEventListener('click', function (event) {
    const button = event.target.closest('.doc-process-btn');
    if (!button || button.disabled) {
      return;
    }

    const processUrl = button.dataset.processUrl;
    const statusUrl = button.dataset.statusUrl;
    const buttons = findRowButtons(button);

    setButtonsBusy(buttons, true);
    button.textContent = 'Starting…';

    fetch(processUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCsrfToken(),
        'X-Requested-With': 'XMLHttpRequest',
      },
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body };
        });
      })
      .then(function (result) {
        if (!result.ok || !result.body.success) {
          setButtonsBusy(buttons, false);
          button.textContent = 'Retry';
          showToast(result.body.error || 'Something went wrong.', true);
          return;
        }

        showToast(result.body.message, false);
        button.textContent = 'Processing…';
        pollStatus(button, statusUrl);
      })
      .catch(function () {
        setButtonsBusy(buttons, false);
        button.textContent = 'Retry';
        showToast('Something went wrong. Please try again.', true);
      });
  });
});
