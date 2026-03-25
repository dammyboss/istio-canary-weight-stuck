FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.1.0
ENV DISPLAY_NUM=1
ENV COMPUTER_HEIGHT_PX=768
ENV COMPUTER_WIDTH_PX=1024
ENV ALLOWED_NAMESPACES="bleater,argocd,monitoring,default,kube-system,field-ops,sandbox,backlog"
ENV ENABLE_ISTIO_BLEATER=true
