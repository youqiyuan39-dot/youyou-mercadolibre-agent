const LOCAL_COLLECT_URL = 'http://127.0.0.1:8789/api/collect';

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'YOUYOU_DETAIL_IMAGES') {
    try {
      const url = new URL(message.url || '');
      if (url.protocol !== 'https:' || (!/(^|\.)(1688\.com|alicdn\.com|tbcdn\.cn)$/i.test(url.hostname) && url.hostname !== 'itemcdn.tmall.com')) {
        sendResponse({ok: false, error: '详情地址不在允许范围'});
        return false;
      }
      fetch(url.toString()).then(async response => {
        if (!response.ok) throw new Error(`详情图读取失败 ${response.status}`);
        const html = (await response.text()).slice(0, 3000000);
        const images = [];
        const seen = new Set();
        const pattern = /(?:src|data-src|data-lazy-load-src)=["']([^"']+)["']|((?:https?:)?\\?\/\\?\/[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%\\-]+\.(?:jpg|jpeg|png|webp)(?:\?[^"'\s<]*)?)/gi;
        let match;
        while ((match = pattern.exec(html)) && images.length < 60) {
          const value = (match[1] || match[2] || '').replace(/\\u002[fF]/g, '/').replace(/\\\//g, '/').replace(/&amp;/g, '&').replace(/^\/\//, 'https://');
          if (!/^https:\/\//i.test(value) || !/alicdn|tbcdn|1688/i.test(value) || seen.has(value)) continue;
          seen.add(value);
          images.push(value);
        }
        sendResponse({ok: true, images});
      }).catch(error => sendResponse({ok: false, error: error.message}));
      return true;
    } catch (error) {
      sendResponse({ok: false, error: error.message});
      return false;
    }
  }
  if (message?.type !== 'YOUYOU_COLLECT') return false;
  if (!/^https:\/\/detail\.1688\.com\/offer\/\d+\.html/i.test(sender.tab?.url || '')) {
    sendResponse({ok: false, error: '只允许采集1688商品详情页'});
    return false;
  }
  fetch(LOCAL_COLLECT_URL, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Youyou-Collector': '1'},
    body: JSON.stringify(message.payload)
  }).then(async response => {
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `本机接收失败 ${response.status}`);
    sendResponse(value);
  }).catch(error => {
    sendResponse({ok: false, error: error.message === 'Failed to fetch' ? '请先打开悠悠商品工作台' : error.message});
  });
  return true;
});
