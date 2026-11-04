# genpic - noobai endpoint
# 日系插画风格 · NoobAI XL
# 模型权重烤进镜像，冷启动无需下载
FROM runpod/worker-comfyui:5.10.0-base

ENV HF_HUB_ENABLE_HF_TRANSFER=1

# handler 校验（和仓库保持一致）
RUN test "$(sha256sum /handler.py | cut -d ' ' -f1)" = \
      "b10d3aa85536b7304c6984e97bc16b0953e411b1092a13880acf4816cd6c542a" \
      || { echo "CHANGED: /handler.py"; exit 1; }

# NoobAI XL v1.1
RUN comfy model download \
      --url https://huggingface.co/Linaqruf/NoobAI-XL-v1.1/resolve/main/NoobAI-XL-v1.1.safetensors \
      --relative-path models/checkpoints \
      --filename NoobAI-XL-v1.1.safetensors

RUN test -s /comfyui/models/checkpoints/NoobAI-XL-v1.1.safetensors && echo "OK: NoobAI loaded"
