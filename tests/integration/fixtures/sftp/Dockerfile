FROM alpine:3.22

RUN apk add --no-cache openssh \
    && addgroup -g 1000 deploy \
    && adduser -D -u 1000 -G deploy -s /bin/sh deploy \
    && addgroup deploy www-data \
    && echo 'deploy:test-only-password' | chpasswd \
    && ssh-keygen -A \
    && mkdir -p /srv/application \
    && chown deploy:www-data /srv/application \
    && chmod 0775 /srv/application

COPY sshd_config /etc/ssh/sshd_config

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D", "-e"]
