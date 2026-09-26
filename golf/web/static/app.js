// Small progressive enhancements; every page works without JavaScript.
(function () {
  'use strict';

  // Poll running jobs and reload once they finish, so results appear without a manual refresh.
  const running = Array.from(document.querySelectorAll('[data-job-id][data-status="running"], [data-job-id][data-status="queued"]'));
  if (running.length) {
    const ids = running.map((el) => el.dataset.jobId);
    const tick = () => {
      Promise.all(ids.map((id) => fetch('/jobs/' + id).then((r) => r.json()).catch(() => ({}))))
        .then((jobs) => {
          if (jobs.every((j) => j.status && j.status !== 'running' && j.status !== 'queued')) {
            if (window.location.search) window.location.replace(window.location.pathname + '#jobs');
            else window.location.reload();
          } else {
            window.setTimeout(tick, 2000);
          }
        });
    };
    window.setTimeout(tick, 1500);
  }

  // Confirmation for destructive or overriding buttons.
  document.querySelectorAll('button[data-confirm]').forEach((b) => {
    b.addEventListener('click', (e) => { if (!window.confirm(b.dataset.confirm)) e.preventDefault(); });
  });

  // Disable submit buttons after submit (uploads take a moment) without losing the clicked button's value.
  document.querySelectorAll('form[data-busy-text]').forEach((f) => {
    f.addEventListener('submit', (e) => {
      const b = e.submitter;
      window.setTimeout(() => {
        f.querySelectorAll('button').forEach((x) => { x.disabled = true; });
        if (b) b.textContent = f.dataset.busyText;
      }, 0);
    });
  });

  // Review grid: mark edited cells so it is clear what "Save corrections" will record.
  document.querySelectorAll('form.review-form').forEach((f) => {
    const mark = (el) => {
      const changed = el.value !== el.dataset.orig;
      el.classList.toggle('changed', changed);
    };
    f.querySelectorAll('input[data-orig], select[data-orig]').forEach((el) => {
      el.addEventListener('input', () => mark(el));
      el.addEventListener('change', () => mark(el));
    });
  });

  // File inputs: show how many files are selected.
  document.querySelectorAll('input[type=file][data-count]').forEach((inp) => {
    const out = document.getElementById(inp.dataset.count);
    inp.addEventListener('change', () => {
      if (out) out.textContent = inp.files.length ? inp.files.length + ' file(s) selected' : '';
    });
  });
})();
