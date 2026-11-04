# genpic - noobai endpoint
# 日系插画风格 · NoobAI XL
# 模型权重烤进镜像，冷启动无需下载
FROM runpod/worker-comfyui:5.10.0-base

ENV HF_HUB_ENABLE_HF_TRANSFER=1

# handler 校验（和仓库保持一致）
RUN test "$(sha256sum /handler.py | cut -d ' ' -f1)" = \
      "c43fcc2bfff6cbb60555d7839b902c54de8a9a55ba20caba2bbd3adf9c2886ec" \
      || { echo "CHANGED: /handler.py"; exit 1; }

# NoobAI XL v1.1
RUN comfy model download \
      --url https://huggingface.co/Laxhar/noobai-XL-1.1/resolve/main/NoobAI-XL-v1.1.safetensors \
      --relative-path models/checkpoints \
      --filename NoobAI-XL-v1.1.safetensors

RUN test -s /comfyui/models/checkpoints/NoobAI-XL-v1.1.safetensors && echo "OK: NoobAI loaded"
