FROM docker:29-dind@sha256:9e79b879b177fbd72c34376a3dd3d91747da5db49e1f560f76aea59561471977 AS docker-cli
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# This opt-in local fixture installs only the SSH daemon and setsid. Package
# lists and OpenSSH are fetched from Debian repositories when the test image is
# built; the test script prints this fact before building.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd /root/.ssh \
    && chmod 0700 /root/.ssh \
    && printf '%s\n' \
        'Port 22' \
        'ListenAddress 0.0.0.0' \
        'HostKey /etc/ssh/ssh_host_ed25519_key' \
        'PermitRootLogin prohibit-password' \
        'PubkeyAuthentication yes' \
        'PasswordAuthentication no' \
        'KbdInteractiveAuthentication no' \
        'UsePAM no' \
        'AuthorizedKeysFile .ssh/authorized_keys' \
        'AllowUsers root' \
        'StrictModes yes' \
        'PidFile /run/sshd.pid' \
        > /etc/ssh/sshd_config

EXPOSE 22
CMD ["/bin/sh", "-c", "ssh-keygen -A && exec /usr/sbin/sshd -D -e"]
