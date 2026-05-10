FROM ubuntu:22.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    openssh-server \
    && rm -rf /var/lib/apt/lists/*

# Create gateway user
RUN useradd -m -s /bin/bash gateway

# Configure SSH for reverse tunnels
RUN mkdir -p /run/sshd && \
    sed -i 's/#GatewayPorts no/GatewayPorts yes/' /etc/ssh/sshd_config && \
    sed -i 's/#Port 22/Port 50555/' /etc/ssh/sshd_config && \
    echo "PermitRootLogin no" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication no" >> /etc/ssh/sshd_config && \
    echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "AllowTcpForwarding yes" >> /etc/ssh/sshd_config && \
    echo "PermitTunnel yes" >> /etc/ssh/sshd_config

# Copy authorized keys for gateway user
RUN mkdir -p /home/gateway/.ssh
COPY authorized_keys /home/gateway/.ssh/authorized_keys
RUN chown -R gateway:gateway /home/gateway/.ssh && \
    chmod 700 /home/gateway/.ssh && \
    chmod 600 /home/gateway/.ssh/authorized_keys

EXPOSE 50555 2222-2231

CMD ["/usr/sbin/sshd", "-D", "-e"]
