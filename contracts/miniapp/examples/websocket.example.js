const config = require('../config/prod.js');

let socket = null;
let retry = 0;

function connect(accessToken, onEvent) {
  const cursor = wx.getStorageSync('blacksoil_ws_cursor') || 0;
  socket = wx.connectSocket({
    url: `${config.wsBaseUrl}/api/v1/mobile/ws?token=${encodeURIComponent(accessToken)}&cursor=${cursor}`
  });
  socket.onOpen(() => { retry = 0; });
  socket.onMessage(message => {
    const event = JSON.parse(message.data);
    if (event.cursor) wx.setStorageSync('blacksoil_ws_cursor', event.cursor);
    if (event.event_id) onEvent(event);
  });
  socket.onClose(() => {
    const delay = Math.min(30000, 1000 * (2 ** retry++));
    setTimeout(() => connect(accessToken, onEvent), delay);
  });
  return socket;
}

function disconnect() {
  if (socket) socket.close();
  socket = null;
}

module.exports = { connect, disconnect };
