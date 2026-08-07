const api = require('./request-client.example.js');

async function loginWithWechat() {
  const loginResult = await new Promise((resolve, reject) => wx.login({ success: resolve, fail: reject }));
  const body = await api.request({
    path: '/api/v1/mobile/auth/wechat',
    method: 'POST',
    data: { code: loginResult.code }
  });
  api.setTokens(body);
  return body.user;
}

async function logout() {
  try {
    await api.request({ path: '/api/v1/mobile/auth/logout', method: 'POST' });
  } finally {
    api.clearTokens();
  }
}

module.exports = { loginWithWechat, logout };
