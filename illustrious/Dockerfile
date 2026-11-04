# genpic - illustrious endpoint
# 日系精致插画风格 · Illustrious XL
# 模型权重烤进镜像，冷启动无需下载
FROM runpod/worker-comfyui:5.10.0-base

ENV HF_HUB_ENABLE_HF_TRANSFER=1

# handler 校验（和仓库保持一致）
RUN test "$(sha256sum /handler.py | cut -d ' ' -f1)" = \
      "b10d3aa85536b7304c6984e97bc16b0953e411b1092a13880acf4816cd6c542a" \
      || { echo "CHANGED: /handler.py"; exit 1; }

# Illustrious XL v1.0
RUN comfy model download \
      --url https://huggingface.co/OnomaAIResearch/Illustrious-XL-v1.0/resolve/main/Illustrious-XL-v1.0.safetensors \
      --relative-path models/checkpoints \
      --filename Illustrious-XL-v1.0.safetensors

RUN test -s /comfyui/models/checkpoints/Illustrious-XL-v1.0.safetensors && echo "OK: Illustrious loaded"
