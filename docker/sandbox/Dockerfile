FROM node:24-bookworm-slim AS node
FROM python:3.12-slim-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && python -m pip install --no-cache-dir pytest \
    && groupadd --gid 1000 kama \
    && useradd --uid 1000 --gid 1000 --create-home kama

ENV HOME=/home/kama \
    PYTHONUSERBASE=/home/kama/.local \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    NPM_CONFIG_PREFIX=/home/kama/.npm-global \
    PATH=/home/kama/.local/bin:/home/kama/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
USER 1000:1000
WORKDIR /workspace
CMD ["/bin/sleep", "infinity"]
