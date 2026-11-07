# Throw-away "fresh box" for testing install.sh: Ubuntu 24.04 with systemd as PID 1.
#   docker build -t aibox-testbox tests/systemd-box
#   docker run -d --name aibox-test --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
#     --tmpfs /run --tmpfs /run/lock -v "$PWD":/kit:ro aibox-testbox
#   docker exec aibox-test /kit/install.sh -y --llama-server /kit/tests/stubs/llama-server --llama-swap /kit/tests/stubs/llama-swap
FROM ubuntu:24.04
ENV container=docker DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv sudo dbus iproute2 procps curl ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/* \
 && systemctl mask systemd-logind.service getty.target console-getty.service systemd-udevd.service systemd-resolved.service 2>/dev/null || true
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
