# 自定义 Caddy 镜像：内置 Cloudflare DNS 插件（用于 DNS-01 签发 Let's Encrypt 证书）
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
