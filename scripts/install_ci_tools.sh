#!/usr/bin/env bash
# Pinned Linux amd64 tools, downloaded into the project. No cluster or root needed.
set -euo pipefail
mkdir -p .tools
curl --fail --silent --show-error --location --retry 3 \
  https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz -o .tools/helm.tar.gz
echo 'a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496  .tools/helm.tar.gz' | sha256sum --check
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/yannh/kubeconform/releases/download/v0.8.0/kubeconform-linux-amd64.tar.gz -o .tools/kubeconform.tar.gz
echo '9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883  .tools/kubeconform.tar.gz' | sha256sum --check
tar -xzf .tools/helm.tar.gz -C .tools linux-amd64/helm
cp .tools/linux-amd64/helm .tools/helm
tar -xzf .tools/kubeconform.tar.gz -C .tools kubeconform
