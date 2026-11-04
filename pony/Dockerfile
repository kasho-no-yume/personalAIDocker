# genpic - pony endpoint
# 日系/插画混合风格 · Pony Diffusion v6 XL
# 模型权重烤进镜像，冷启动无需下载
FROM runpod/worker-comfyui:5.10.0-base

ENV HF_HUB_ENABLE_HF_TRANSFER=1

# handler 校验（和仓库保持一致）
RUN test "$(sha256sum /handler.py | cut -d ' ' -f1)" = \
      "b10d3aa85536b7304c6984e97bc16b0953e411b1092a13880acf4816cd6c542a" \
      || { echo "CHANGED: /handler.py"; exit 1; }

# Pony Diffusion v6 XL
RUN comfy model download \
      --url https://huggingface.co/AstraliteHeart/pony-diffusion-v6-xl/resolve/main/ponyDiffusionV6XL_v6StartWithThisOne.safetensors \
      --relative-path models/checkpoints \
      --filename ponyDiffusionV6XL_v6StartWithThisOne.safetensors

RUN test -s /comfyui/models/checkpoints/ponyDiffusionV6XL_v6StartWithThisOne.safetensors && echo "OK: Pony loaded"
