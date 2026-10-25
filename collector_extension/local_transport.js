// All content-to-loopback traffic goes through the service worker.
globalThis.YouyouLocal = globalThis.YouyouLocal || {
 async send(message) {
  let response;
  try { response = await chrome.runtime.sendMessage(message); }
  catch(e) { throw Error('[content→background] '+e.message+'；扩展已更新请刷新本页（智赢/1688）；仍失败请重新加载扩展。'); }
  if(!response) throw Error('[content→background] 后台未响应/未注册消息 '+message.type+'；扩展已更新请刷新本页，并确认后台版本与扩展一致。');
  if(!response.ok) throw Error('['+(response.step||'background')+'] '+(response.error||'空错误')+'；'+(response.hint||'请重新加载扩展并刷新本页'));
  return response;
 }
};
