# BOXP-546 lab image. Built and run only inside a run workspace with --network none.
ARG BASE
FROM ${BASE}
COPY bb /usr/local/bin/bb
COPY stub.bb /lab/stub.bb
COPY ticket.md /lab/vault/Tickets/BOXP-9001.md
COPY config.yaml webhook_subscriptions.json /opt/data/
COPY plugin/ /opt/data/plugins/
RUN chmod 0755 /usr/local/bin/bb && mkdir -p /lab/logs /lab/vault/Projects \
    && chmod -R a+rwX /lab /opt/data
ENV HOME=/opt/data \
    WEBHOOK_ENABLED=true WEBHOOK_SECRET=lab-secret \
    BOXP_NOTIFY_ENABLED=1 BOXP_NOTIFY_VAULT=/lab/vault \
    BOXP_NOTIFY_TELEGRAM_MODE=fallback BOXP_NOTIFY_TELEGRAM_API_BASE=http://127.0.0.1:18080 \
    BOXP_NOTIFY_TELEGRAM_TOKEN=lab-token BOXP_NOTIFY_TELEGRAM_CHAT_ID=lab-chat \
    BOXP_NOTIFY_RECOVERY_SECONDS=10 LAB_LOG_DIR=/lab/logs
