const api = require('./request-client.example.js');
const STORAGE_KEY = 'blacksoil_offline_outbox_v1';

function newIdempotencyKey() {
  return 'wx-' + Date.now() + '-' + Math.random().toString(36).slice(2, 12);
}

function enqueue(request) {
  const queue = wx.getStorageSync(STORAGE_KEY) || [];
  queue.push({
    id: newIdempotencyKey(),
    request,
    attempts: 0,
    nextAttemptAt: Date.now()
  });
  wx.setStorageSync(STORAGE_KEY, queue);
}

async function flush() {
  const queue = wx.getStorageSync(STORAGE_KEY) || [];
  const remaining = [];
  for (const item of queue) {
    if (item.nextAttemptAt > Date.now()) {
      remaining.push(item);
      continue;
    }
    try {
      await api.request({
        ...item.request,
        headers: { ...(item.request.headers || {}), 'Idempotency-Key': item.id }
      });
    } catch (error) {
      if (error.code === 'VERSION_CONFLICT' || error.code === 'IDEMPOTENCY_CONFLICT') {
        item.blocked = true;
        item.error = { code: error.code, traceId: error.traceId };
      } else {
        item.attempts += 1;
        item.nextAttemptAt = Date.now() + Math.min(300000, 1000 * (2 ** item.attempts));
      }
      remaining.push(item);
    }
  }
  wx.setStorageSync(STORAGE_KEY, remaining);
}

wx.onNetworkStatusChange(status => {
  if (status.isConnected) flush();
});

module.exports = { enqueue, flush, newIdempotencyKey };
