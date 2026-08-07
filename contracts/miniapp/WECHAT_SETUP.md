# 微信后台与真机联调

1. 在小程序后台配置合法域名：
   - request：`https://api.flexibility607.cn`
   - uploadFile：`https://api.flexibility607.cn`
   - downloadFile：`https://api.flexibility607.cn`
   - socket：`wss://api.flexibility607.cn`
2. 域名必须完成备案、可信 TLS 和微信校验；正式环境不能使用 `47.94.3.39` 或关闭域名校验。
3. 在服务器配置 `WECHAT_APP_ID`、`WECHAT_APP_SECRET`，禁止写入小程序代码或 Git。
4. 配置位置权限与隐私声明，说明用途、频率、保存范围和关闭方式；只有运输中任务开启持续定位。
5. 图片上传前向用户说明用途，客户端压缩并限制为 JPG/PNG/WebP，服务端上限 10 MB。
6. 真机分别验证 4G、Wi-Fi、切后台、断网、Token 过期、WSS 重连和离线队列重放。
7. 开发模式可使用 `demo:driver_demo` 等 code；生产模式会禁用 demo 登录。
