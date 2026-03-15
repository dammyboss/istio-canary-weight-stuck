# ==========================================================
# Stage 1: Download kubectl image using skopeo
# ==========================================================
FROM quay.io/skopeo/stable:v1.21.0 AS image-fetcher

WORKDIR /images

# Download kubectl image for PostSync hook and drift enforcers
RUN skopeo copy \
    docker://alpine/k8s:1.31.4 \
    docker-archive:k8s-1.31.4.tar:alpine/k8s:1.31.4

# ==========================================================
# Stage 2: Final image with pre-downloaded images
# ==========================================================
FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.0.3
ENV DISPLAY_NUM=1
ENV COMPUTER_HEIGHT_PX=768
ENV COMPUTER_WIDTH_PX=1024
ENV ALLOWED_NAMESPACES="bleater,argocd"
ENV ENABLE_ISTIO_BLEATER=true

# Copy to K3s auto-import directory - K3s will automatically import on startup
COPY --from=image-fetcher /images/*.tar /var/lib/rancher/k3s/agent/images/
