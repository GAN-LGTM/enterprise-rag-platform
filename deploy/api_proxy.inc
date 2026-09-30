# API 反向代理公共片段（由 nginx.conf 通过 include 引用）
proxy_pass http://api:8000;
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

# ---- SSE 流式输出必需配置 ----
proxy_buffering off;              # 关闭响应缓冲，保证逐帧输出
proxy_cache off;
proxy_read_timeout 3600s;         # 长连接不被掐断
proxy_send_timeout 3600s;
chunked_transfer_encoding on;
add_header X-Accel-Buffering no;
