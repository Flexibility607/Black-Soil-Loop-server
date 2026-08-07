const api = require('./request-client.example.js');
const config = require('../config/prod.js');
const { newIdempotencyKey } = require('./offline-outbox.example.js');

async function uploadLocation(taskId, location) {
  return api.request({
    path: `/api/v1/mobile/tasks/${taskId}/locations`,
    method: 'POST',
    headers: { 'Idempotency-Key': newIdempotencyKey() },
    data: {
      latitude: location.latitude,
      longitude: location.longitude,
      speed_mps: location.speed || 0,
      accuracy_m: location.accuracy || null,
      recorded_at: new Date().toISOString()
    }
  });
}

function uploadImage(filePath, purpose, accessToken) {
  return new Promise((resolve, reject) => {
    wx.uploadFile({
      url: `${config.apiBaseUrl}/api/v1/mobile/uploads?purpose=${purpose}`,
      filePath,
      name: 'file',
      header: { Authorization: 'Bearer ' + accessToken },
      success: result => {
        const body = JSON.parse(result.data);
        if (result.statusCode === 201) resolve(body.attachment);
        else reject(Object.assign(new Error(body.message), { code: body.code, traceId: body.trace_id }));
      },
      fail: reject
    });
  });
}

module.exports = { uploadLocation, uploadImage };
