(() => {
  if (document.getElementById('youyou-collector-root')) return;

  const root = document.createElement('div');
  root.id = 'youyou-collector-root';
  root.style.cssText = 'position:fixed;right:18px;top:34%;z-index:2147483647;font-family:system-ui,"Microsoft YaHei",sans-serif;color:#1f2937';

  const panel = document.createElement('div');
  panel.style.cssText = 'display:none;width:270px;margin-bottom:8px;padding:14px;border:1px solid #dbeafe;border-radius:11px;background:#fff;box-shadow:0 8px 28px rgba(15,23,42,.22);font-size:13px';
  panel.innerHTML = `
    <strong style="display:block;margin-bottom:4px">选择采集方式</strong>
      <small style="display:block;margin-bottom:10px;color:#64748b">悠悠采集 0.7.1</small>
    <div style="display:flex;gap:8px">
      <button type="button" data-mode="multi">多 SKU 采集</button>
      <button type="button" data-mode="single">单 SKU 采集</button>
    </div>
    <details style="margin-top:12px" open>
      <summary style="cursor:pointer;font-weight:600">人工包装规格（可选）</summary>
      <label style="display:block;margin-top:9px">包装重量（kg）<input data-weight type="number" min="0" step="0.001" placeholder="例如 0.2"></label>
      <label style="display:block;margin-top:8px">包装尺寸（长-宽-高，cm）<input data-size placeholder="例如 10-10-10"></label>
      <small style="display:block;margin-top:7px;color:#64748b">可留空：默认由AI识别；手填后以手填值为准。</small>
    </details>
    <div data-status style="display:none;margin-top:10px;line-height:1.45"></div>`;
  panel.querySelectorAll('button[data-mode]').forEach(node => {
    node.style.cssText = 'flex:1;border:1px solid #bfdbfe;border-radius:8px;padding:9px 5px;background:#f8fbff;color:#1d4ed8;cursor:pointer;font-weight:600';
  });
  panel.querySelectorAll('input').forEach(node => {
    node.style.cssText = 'box-sizing:border-box;width:100%;margin-top:4px;padding:8px;border:1px solid #cbd5e1;border-radius:7px';
  });

  const button = document.createElement('button');
  button.type = 'button';
  button.innerHTML = `<img src="${chrome.runtime.getURL('youyou-logo.png')}" alt="" width="24" height="24" style="mix-blend-mode:screen">悠悠采集`;
  button.title = '打开悠悠采集';
  button.style.cssText = 'float:right;display:flex;align-items:center;gap:7px;border:0;border-radius:9px;padding:11px 16px;background:#087df1;color:#fff;box-shadow:0 5px 16px rgba(8,125,241,.32);font-size:15px;font-weight:700;cursor:pointer';

  const status = panel.querySelector('[data-status]');
  const modeButtons = [...panel.querySelectorAll('button[data-mode]')];
  const showStatus = (message, error = false) => {
    status.textContent = message;
    status.style.display = 'block';
    status.style.color = error ? '#b91c1c' : '#166534';
  };

  const applyManualPackage = payload => {
    const weightKg = Number(panel.querySelector('[data-weight]').value || 0);
    const sizeText = panel.querySelector('[data-size]').value.trim();
    const dimensions = sizeText ? sizeText.split(/[-×xX,，\s]+/).map(Number) : [];
    payload.package_input_mode = weightKg > 0 || sizeText ? 'manual' : 'ai_auto';
    if (weightKg > 0) {
      payload.package.weight_g = Math.round(weightKg * 1000);
      payload.package.evidence = 'USER_INPUT_UNVERIFIED';
    }
    if (dimensions.length === 3 && dimensions.every(value => value > 0)) {
      [payload.package.length_cm, payload.package.width_cm, payload.package.height_cm] = dimensions;
      payload.package.evidence = 'USER_INPUT_UNVERIFIED';
    } else if (sizeText) {
      throw new Error('包装尺寸请按“长-宽-高”填写，例如10-10-10');
    }
  };

  const collect = async mode => {
    modeButtons.forEach(node => { node.disabled = true; node.style.opacity = '.6'; });
    showStatus('正在加载商品资料并分类图片…');
    try {
      const payload = await collectVisible1688Product();
      payload.collection_mode = mode;
      applyManualPackage(payload);
      const result = await chrome.runtime.sendMessage({type: 'YOUYOU_COLLECT', payload});
      if (!result?.ok) throw new Error(result?.error || '采集失败');
      const counts = result.image_counts || {};
      const modeText = mode === 'multi' ? '多SKU' : '单SKU';
      const packageText = result.package_ai_status === 'REQUEST_PREPARED' ? '，AI包装识别已排队' : '';
      showStatus(`成功：${modeText}，商品图${counts.product || 0}张、SKU图${counts.sku || 0}张、详情图${counts.detail || 0}张${packageText}。已进入处理队列。`);
    } catch (error) {
      showStatus(`失败：${error.message}`, true);
    } finally {
      modeButtons.forEach(node => { node.disabled = false; node.style.opacity = '1'; });
    }
  };

  modeButtons.forEach(node => node.addEventListener('click', () => collect(node.dataset.mode)));
  button.addEventListener('click', () => {
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  });

  root.append(panel, button);
  document.documentElement.appendChild(root);
})();
