/* Choose recommendations; generation is explicit and never sends an email. */
(() => {
  const senderFields = [...document.querySelectorAll('[form="sender-form"]')];
  const senderValue = () => JSON.stringify(senderFields.map(input => input.value));
  const initialSender = senderValue();
  let senderDirty = false;
  const updateSender = () => {
    const vendor = document.querySelector('[data-sender-vendor]')?.value;
    if (!vendor) return;
    document.querySelector('[data-sender-custom]').hidden = vendor !== 'custom';
    document.querySelector('[data-sender-context]').hidden = vendor === 'neutral';
    senderDirty = senderValue() !== initialSender;
    document.querySelector('[data-sender-dirty]').hidden = !senderDirty;
    selection?.dispatchEvent(new Event('change'));
  };
  const selection = document.querySelector('[data-selection-form]');
  if (selection) {
    const count = selection.querySelector('[data-selection-count]');
    const submit = selection.querySelector('button[type=submit]');
    const update = () => {
      const total = selection.querySelectorAll('input[name=selected]:checked').length;
      count.textContent = total ? `${total}개 선택 · 제안서와 메일을 함께 만듭니다.` : '1~3개를 선택해 주세요.';
      submit.disabled = total < 1 || total > 3 || selection.querySelector('fieldset').disabled || senderDirty;
    };
    selection.addEventListener('change', event => {
      if (selection.querySelectorAll('input[name=selected]:checked').length > 3) {
        event.target.checked = false;
        count.textContent = '최대 3개까지 선택할 수 있습니다.';
        return;
      }
      update();
    });
    update();
  }
  senderFields.forEach(field => field.addEventListener('input', updateSender));
  senderFields.forEach(field => field.addEventListener('change', updateSender));
  // Prevent implicit Enter submission from bypassing changed seller context.
  selection?.addEventListener('submit', event => { if (senderDirty) event.preventDefault(); });
  document.querySelectorAll('[data-generate-form]').forEach(form => form.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    form.querySelector('button[type=submit]').disabled = true;
  }));
  document.querySelectorAll('.prep-refs a, .prep-source-link').forEach(link => link.addEventListener('click', () => {
    let detail = document.getElementById(link.hash.slice(1));
    while (detail) {
      if (detail.tagName === 'DETAILS') detail.open = true;
      detail = detail.parentElement;
    }
  }));
  document.querySelector('[data-copy-email]')?.addEventListener('click', async () => {
    const status = document.querySelector('#copy-status');
    try {
      await navigator.clipboard.writeText(`제목: ${document.querySelector('#email-subject').textContent}\n\n${document.querySelector('#email-text').textContent}`);
      status.textContent = '복사했습니다.';
    } catch (_) { status.textContent = '본문을 선택해 복사해 주세요.'; }
  });
  const progress = document.querySelector('[data-prep-poll]');
  if (progress) {
    const poll = async () => {
      try {
        const response = await fetch(progress.dataset.prepPoll, {headers: {Accept: 'application/json'}});
        if (response.redirected) { location.reload(); return; }
        if (response.ok) {
          const data = await response.json();
          if (data.job?.state !== 'running') {
            const anchor = data.job?.state === 'done' ? (data.job.step === 'package' ? '#documents' : '#usecases') : '';
            history.replaceState(null, '', location.pathname + anchor);
            location.reload();
            return;
          }
        }
      } catch (_) { /* Preserve the saved result during temporary disconnection. */ }
      setTimeout(poll, 4000);
    };
    setTimeout(poll, 4000);
  }
})();
