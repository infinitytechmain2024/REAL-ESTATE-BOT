# Hostinger VPS hardening checklist

Complete these controls before placing real credentials or browser profiles on the VPS.

- Create a non-root deployment user; disable SSH password authentication and root login; use distinct SSH keys.
- Enable a host firewall: allow SSH only from administration IPs and allow 80/443 only when Caddy is ready. Do not expose PostgreSQL (5432), Redis (6379), Chrome CDP (9222), VNC/noVNC (5900/6080), or internal application ports.
- Keep Docker's published ports loopback-only by default. Publish only Caddy after DNS, TLS and access policy are configured.
- Apply automatic security updates and schedule Docker image updates; reboot after kernel security updates.
- Put `.env` in owner-only storage (`chmod 600 .env`), never commit it, rotate API tokens on personnel/device changes, and use a secret manager when available.
- Back up PostgreSQL with encrypted, tested off-host backups. Persist and test restore procedures; Redis is a queue/cache and is not the system of record.
- Restrict Docker socket access to the deployment user. Do not install unreviewed compose files or grant `privileged`, host networking, or Docker socket mounts to collectors.
- Use a separate persistent browser profile per permitted platform, encrypted disk where possible. Human verification is required on challenges; never automate CAPTCHA/checkpoint bypasses.
- Access remote browser verification through Tailscale or another authenticated private gateway. Never publish Chrome debugging or noVNC directly.
- Centralize Docker/Caddy logs, retain audit logs according to policy, monitor disk, memory, failed jobs, backup freshness and authentication events.
- Before production, replace Caddy's loopback listener with a domain/TLS configuration and put authentication/rate limits in front of every route.
