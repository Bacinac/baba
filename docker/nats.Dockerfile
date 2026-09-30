# nats-server with BABA's config baked in, plus htpasswd so the start script can
# hand the server bcrypt hashes of the passwords the clients use, and openssl for
# the WebSocket listener's certificate.
FROM nats:2.15-alpine
RUN apk add --no-cache apache2-utils openssl
COPY nats.conf nats-ws.conf nats-auth.conf /etc/nats/
COPY --chmod=755 nats-start.sh /usr/local/bin/baba-nats-start
ENTRYPOINT ["baba-nats-start"]
