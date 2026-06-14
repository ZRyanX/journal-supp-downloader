FROM alpine:3.19

RUN apk add --no-cache tor obfs4proxy

# Create necessary directories
RUN mkdir -p /var/lib/tor /etc/tor

# Default torrc with obfs4 bridge support
COPY torrc /etc/tor/torrc

# Run as tor user
USER tor

EXPOSE 9050

CMD ["tor", "-f", "/etc/tor/torrc"]
