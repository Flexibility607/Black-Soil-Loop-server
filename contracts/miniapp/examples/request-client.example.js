const config = require('../config/prod.js');

let accessToken = '';
let refreshPromise = null;

function rawRequest({ path, method = 'GET', data, headers = {} }) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: config.apiBaseUrl + path,
      method,
      data,
      header: headers,
      success: resolve,
      fail: reject
    });
  });
}

async function refreshAccessToken() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const refreshToken = wx.getStorageSync('blacksoil_refresh_token');
    if (!refreshToken) throw new Error('登录已过期');
    const res = await rawRequest({
      path: '/api/v1/mobile/auth/refresh',
      method: 'POST',
      data: { refresh_token: refreshToken }
    });
    if (res.statusCode !== 200) throw Object.assign(new Error(res.data.message), { response: res });
    accessToken = res.data.access_token;
    wx.setStorageSync('blacksoil_refresh_token', res.data.refresh_token);
    return accessToken;
  })().finally(() => { refreshPromise = null; });
  return refreshPromise;
}

async function request(options, retried = false) {
  const headers = { ...(options.headers || {}) };
  if (accessToken) headers.Authorization = 'Bearer ' + accessToken;
  const res = await rawRequest({ ...options, headers });
  if (res.statusCode === 401 && !retried) {
    await refreshAccessToken();
    return request(options, true);
  }
  if (res.statusCode < 200 || res.statusCode >= 300) {
    throw Object.assign(new Error(res.data.message || '请求失败'), {
      code: res.data.code,
      details: res.data.details,
      traceId: res.data.trace_id,
      statusCode: res.statusCode
    });
  }
  return res.data;
}

function setTokens(body) {
  accessToken = body.access_token;
  wx.setStorageSync('blacksoil_refresh_token', body.refresh_token);
}

function clearTokens() {
  accessToken = '';
  wx.removeStorageSync('blacksoil_refresh_token');
}

module.exports = { request, setTokens, clearTokens };
